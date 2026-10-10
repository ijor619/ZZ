"""
Zig_Zag lounge — бот заказов для кальянной (единый файл).

Запуск:      python bot.py
Зависимости: pip install -r requirements.txt
Меню:        menu.json (лежит рядом с bot.py)
Настройки:   переменные окружения BOT_TOKEN, CHANNEL_ID, ADMIN_IDS, CURRENCY
             (на Bothost — раздел «Переменные»; локально — файл .env)
"""

from __future__ import annotations

import asyncio
import base64
import calendar
import html
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("zigzag")

BOT_VERSION = "2026-10-10-v4"

# Все временные метки бота — московские (UTC+3, переход на летнее время отменён)
_MSK = timezone(timedelta(hours=3), name="MSK")


def _now_dt() -> datetime:
    """Текущие дата и время по Москве (наивные — как и все метки в БД)."""
    return datetime.now(_MSK).replace(tzinfo=None)

# ============================== НАСТРОЙКИ ==================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CURRENCY = (os.getenv("CURRENCY", "₽") or "₽").strip()


def _parse_channel_id(raw: str):
    """CHANNEL_ID может быть числом (-100...) или @username."""
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


CHANNEL_ID = _parse_channel_id(os.getenv("CHANNEL_ID", "").strip())
ADMIN_IDS: set[int] = {
    int(x)
    for x in os.getenv("ADMIN_IDS", "").replace(";", ",").replace(" ", ",").split(",")
    if x.strip().lstrip("-").isdigit()
}


def _data_dir() -> str:
    """Каталог данных: DATA_DIR (Bothost даёт /app/data), иначе /app/data в Docker, иначе cwd."""
    d = os.getenv("DATA_DIR") or ("/app/data" if os.path.isdir("/app") else ".")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = "."  # диск только на чтение — пишем рядом с кодом
    return d


DB_PATH = os.path.join(_data_dir(), "orders.db")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Целое из переменной окружения с проверкой диапазона (мусор → default)."""
    try:
        v = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return v if lo <= v <= hi else default


# --- Лояльность (кешбэк баллами, 1 балл = 1 ₽) ---
LOYALTY_PERCENT = _env_int("LOYALTY_PERCENT", 5, 0, 50)        # % кешбэка от оплаты
BONUS_MAX_PAY_PCT = _env_int("BONUS_MAX_PAY_PCT", 50, 0, 100)  # макс. % чека баллами

# --- Защита от спама и злоупотреблений (админы из ADMIN_IDS не ограничены) ---
ORDER_MAX_ITEMS = 20         # максимум позиций (штук) в одном заказе
ORDER_COOLDOWN_SEC = 60      # не чаще одного заказа в минуту от гостя
RSV_MAX_ACTIVE_PER_USER = 2  # активных броней («Ожидает»/«Подтверждена») на гостя
FSM_TTL_HOURS = 12           # через сколько часов бездействия сбрасывать стол/корзину
RESERVE_HOURS = 24           # резерв склада держат только открытые заказы за 24 ч


def _is_admin(user_id: int | None) -> bool:
    return bool(user_id) and int(user_id) in ADMIN_IDS

# ================================== БД ======================================


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%d %H:%M:%S")


def db_init() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                table_no  TEXT    NOT NULL,
                total     INTEGER NOT NULL,
                status    TEXT    NOT NULL DEFAULT 'accepted',
                created_at TEXT   NOT NULL,
                updated_at TEXT   NOT NULL,
                message_id INTEGER,
                user_id   INTEGER,
                username  TEXT,
                phone     TEXT,
                issued_at TEXT,
                pay_method TEXT,
                stock_deducted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS order_items (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                item_id  TEXT,
                name     TEXT    NOT NULL,
                price    INTEGER NOT NULL,
                qty      INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingredients (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL UNIQUE,
                unit    TEXT NOT NULL DEFAULT 'шт',
                qty     REAL NOT NULL DEFAULT 0,
                cost    REAL NOT NULL DEFAULT 0,
                min_qty REAL NOT NULL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS recipes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id   TEXT NOT NULL,
                ingredient_id INTEGER NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
                qty       REAL NOT NULL,
                UNIQUE (item_id, ingredient_id)
            );
            CREATE TABLE IF NOT EXISTS stock_moves (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ingredient_id INTEGER NOT NULL,
                delta         REAL NOT NULL,
                reason        TEXT NOT NULL,
                ref           TEXT,
                created_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS reservations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT    NOT NULL,
                date       TEXT    NOT NULL,
                time       TEXT    NOT NULL,
                guests     INTEGER NOT NULL,
                duration   TEXT,
                phone      TEXT,
                guest_name TEXT,
                user_id    INTEGER,
                username   TEXT,
                status     TEXT    NOT NULL DEFAULT 'new',
                created_at TEXT    NOT NULL,
                updated_at TEXT    NOT NULL,
                message_id INTEGER,
                actual_min INTEGER,
                price      INTEGER,
                pay_method TEXT
            );
            """
        )
        try:
            conn.execute("ALTER TABLE reservations ADD COLUMN duration TEXT")
        except sqlite3.OperationalError:
            pass  # колонка уже есть (обновление существующей БД)
        for _stmt in (
            "ALTER TABLE orders ADD COLUMN phone TEXT",
            "ALTER TABLE reservations ADD COLUMN phone TEXT",
            "ALTER TABLE orders ADD COLUMN issued_at TEXT",
            "ALTER TABLE orders ADD COLUMN pay_method TEXT",
            "ALTER TABLE order_items ADD COLUMN item_id TEXT",
            "ALTER TABLE orders ADD COLUMN stock_deducted INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE reservations ADD COLUMN actual_min INTEGER",
            "ALTER TABLE reservations ADD COLUMN price INTEGER",
            "ALTER TABLE reservations ADD COLUMN pay_method TEXT",
            # заказ по акции / оплата баллами / кешбэк
            "ALTER TABLE orders ADD COLUMN kind TEXT NOT NULL DEFAULT 'regular'",
            "ALTER TABLE orders ADD COLUMN bonus_pending INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN bonus_used INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN bonus_accrued INTEGER NOT NULL DEFAULT 0",
            # предзаказ: время прихода и (необязательно) бронь, к которой он
            "ALTER TABLE orders ADD COLUMN arrive_at TEXT",
            "ALTER TABLE orders ADD COLUMN rsv_id INTEGER",
        ):
            try:
                conn.execute(_stmt)
            except sqlite3.OperationalError:
                pass  # колонка уже есть
        conn.executescript(
            """
            -- карта лояльности гостя (ключ — Telegram id; телефон = номер карты)
            CREATE TABLE IF NOT EXISTS guests (
                user_id    INTEGER PRIMARY KEY,
                phone      TEXT,
                phone_verified INTEGER NOT NULL DEFAULT 0,
                username   TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_guests_phone
                ON guests(phone) WHERE phone IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_guests_username ON guests(username);
            -- журнал баллов: баланс = SUM(delta)
            CREATE TABLE IF NOT EXISTS bonus_moves (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                delta      INTEGER NOT NULL,
                kind       TEXT NOT NULL,
                amount     INTEGER,
                order_id   INTEGER,
                admin_id   INTEGER,
                note       TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_bonus_user ON bonus_moves(user_id);
            -- состояние диалогов (FSM) — переживает перезапуск бота
            CREATE TABLE IF NOT EXISTS fsm (
                k          TEXT PRIMARY KEY,
                state      TEXT,
                data       TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user_id, created_at);
            CREATE INDEX IF NOT EXISTS ix_rsv_user ON reservations(user_id, status);
            """
        )


def db_add_order(
    table_no: str,
    total: int,
    user_id: int | None,
    username: str | None,
    phone: str | None = None,
    kind: str = "regular",
    arrive_at: str | None = None,
    rsv_id: int | None = None,
) -> int:
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO orders (table_no, total, status, created_at, updated_at,"
            " user_id, username, phone, kind, arrive_at, rsv_id)"
            " VALUES (?, ?, 'accepted', ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(table_no), int(total), now, now, user_id, username, phone, kind,
             arrive_at, rsv_id),
        )
        return int(cur.lastrowid)


def db_add_items(order_id: int, items: list[dict]) -> None:
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO order_items (order_id, item_id, name, price, qty)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (order_id, i.get("id"), i["name"], int(i["price"]), int(i["qty"]))
                for i in items
            ],
        )


def db_delete_order(order_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))


def db_set_message_id(order_id: int, message_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE orders SET message_id = ? WHERE id = ?", (message_id, order_id))


def db_set_status(order_id: int, status: str) -> None:
    now = _now()
    with _connect() as conn:
        if status == "issued":
            # запоминаем время выдачи — для статистики /timing
            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ?, issued_at = ? WHERE id = ?",
                (status, now, now, order_id),
            )
        elif status in ("accepted", "ready", "cancelled"):
            # заказ вернули в работу/отменили — сбрасываем время выдачи
            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ?, issued_at = NULL WHERE id = ?",
                (status, now, order_id),
            )
        else:
            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, order_id),
            )


def db_set_pay_method(order_id: int, method: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE orders SET pay_method = ? WHERE id = ?", (method, order_id))


def db_claim_stock_flag(order_id: int, new_val: int) -> bool:
    """Атомарно переключает флаг списания склада. True — переключили мы
    (значит, списывать/возвращать нужно именно сейчас); False — уже было.
    Защищает от двойного списания при одновременных нажатиях."""
    old_val = 0 if new_val else 1
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE orders SET stock_deducted = ? WHERE id = ? AND stock_deducted = ?",
            (int(new_val), order_id, old_val),
        )
        return cur.rowcount == 1


def db_last_order_age_sec(user_id: int) -> float | None:
    """Сколько секунд назад гость оформил последний заказ (None — не было)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT created_at FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    try:
        t = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return (_now_dt() - t).total_seconds()


def db_timing_today() -> tuple[list[dict], list[dict]]:
    """Статистика для /timing: выданные сегодня и в работе прямо сейчас."""
    today = _now_dt().strftime("%Y-%m-%d")
    with _connect() as conn:
        issued = conn.execute(
            "SELECT id, table_no, created_at, issued_at FROM orders"
            " WHERE created_at LIKE ? AND issued_at IS NOT NULL"
            " AND kind != 'pre'"  # предзаказ готовят к приходу — скорость не про него
            " ORDER BY issued_at",
            (today + "%",),
        ).fetchall()
        active = conn.execute(
            "SELECT id, table_no, created_at, status FROM orders"
            " WHERE created_at LIKE ? AND status IN ('accepted', 'ready')"
            " AND kind != 'pre'"
            " ORDER BY created_at",
            (today + "%",),
        ).fetchall()
    return [dict(r) for r in issued], [dict(r) for r in active]


def db_get_order(order_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            return None
        items = conn.execute(
            "SELECT item_id, name, price, qty FROM order_items WHERE order_id = ?",
            (order_id,),
        ).fetchall()
    order = dict(row)
    order["items"] = [dict(i) for i in items]
    return order


def db_today_summary() -> dict:
    today = _now_dt().strftime("%Y-%m-%d")
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(total), 0) AS revenue"
            " FROM orders WHERE created_at LIKE ? AND status != 'cancelled'",
            (today + "%",),
        ).fetchone()
        by_status = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM orders"
            " WHERE created_at LIKE ? GROUP BY status",
            (today + "%",),
        ).fetchall()
    return {
        "date": today,
        "count": row["cnt"],
        "revenue": row["revenue"],
        "by_status": {r["status"]: r["cnt"] for r in by_status},
    }


def db_recent_orders(limit: int = 10) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, table_no, total, status, created_at FROM orders"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------ брони (reservation db) ------------------------
def db_add_reservation(
    kind: str,
    date_s: str,
    time_s: str,
    guests: int,
    duration: str,
    guest_name: str,
    user_id: int | None,
    username: str | None,
    phone: str | None = None,
) -> int:
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO reservations (kind, date, time, guests, duration,"
            " guest_name, user_id, username, phone, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
            (
                kind,
                date_s,
                time_s,
                int(guests),
                duration,
                guest_name,
                user_id,
                username,
                phone,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def db_get_reservation(rid: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM reservations WHERE id = ?", (rid,)).fetchone()
    return dict(row) if row else None


def db_delete_reservation(rid: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM reservations WHERE id = ?", (rid,))


def db_set_rsv_status(rid: int, status: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE reservations SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), rid),
        )


def db_set_rsv_message_id(rid: int, message_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE reservations SET message_id = ? WHERE id = ?",
            (message_id, rid),
        )


def db_set_rsv_actual(rid: int, actual_min: int, price: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE reservations SET actual_min = ?, price = ? WHERE id = ?",
            (int(actual_min), int(price), rid),
        )


def db_set_rsv_paid(rid: int, method: str, actual_min: int, price: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE reservations SET status = 'paid', pay_method = ?,"
            " actual_min = ?, price = ?, updated_at = ? WHERE id = ?",
            (method, int(actual_min), int(price), _now(), rid),
        )


def db_rsv_active_count(kind: str, date_s: str) -> int:
    """Активные брони категории на дату: «Ожидает» и «Подтверждена»."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM reservations"
            " WHERE kind = ? AND date = ? AND status IN ('new', 'confirmed')",
            (kind, date_s),
        ).fetchone()
    return int(row["n"])


def db_rsv_user_active(user_id: int) -> int:
    """Активные брони гостя («Ожидает»/«Подтверждена») на сегодня и позже
    (вчерашняя дата тоже — её ночные слоты ещё могут быть впереди)."""
    since = (_now_dt() - timedelta(days=1)).strftime("%Y-%m-%d")
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM reservations WHERE user_id = ?"
            " AND status IN ('new', 'confirmed') AND date >= ?",
            (user_id, since),
        ).fetchone()
    return int(row["n"])


def db_add_reservation_checked(user_limit: int | None, **kw) -> tuple[int | None, str]:
    """Проверка лимитов и вставка брони одной синхронной операцией (без await
    между проверкой и записью — гонки двух гостей за последнее место нет).
    Возвращает (id, "") или (None, "full" | "user_limit")."""
    kind, date_s, uid = kw["kind"], kw["date_s"], kw.get("user_id")
    time_s, code = kw["time_s"], kw.get("duration") or ""
    try:
        late = _arrival(date_s, time_s) <= _now_dt()
    except ValueError:
        return None, "full"
    if (
        late
        or not rsv_fits_closing(kind, date_s, time_s, code)
        or not _rsv_free_in(db_rsv_active_rows(kind, date_s), kind, date_s, time_s, code)
    ):
        return None, "full"
    if user_limit is not None and uid and db_rsv_user_active(uid) >= user_limit:
        return None, "user_limit"
    return db_add_reservation(**kw), ""


# ---------------------------------------------- брони по интервалам времени
def _rsv_close_dt(date_s: str) -> datetime:
    """Закрытие рабочего дня date_s: следующий календарный день, 03:00."""
    d = datetime.strptime(date_s, "%Y-%m-%d") + timedelta(days=1)
    return d.replace(hour=RSV_CLOSE_H, minute=0, second=0, microsecond=0)


def _rsv_need_min(kind: str, code: str) -> int:
    """Сколько минут должно оставаться до закрытия, чтобы вариант был доступен."""
    if kind in RSV_ADJ_KINDS:
        return RSV_BOOKED_MIN.get(code, 120)
    if kind == "bar":
        return RSV_BAR_NEED_MIN
    return RSV_DUR_NEED_MIN.get(code, 60)


def rsv_interval(kind: str, date_s: str, time_s: str, code: str,
                 actual_min: int | None = None) -> tuple[datetime, datetime]:
    """Сколько бронь занимает места: (начало, конец). VIP/PS — пакет или факт
    после правки ±15 мин; стол/бар — расчётно, но не дольше закрытия."""
    start = _arrival(date_s, time_s)
    if kind in RSV_ADJ_KINDS:
        occ = int(actual_min or 0) or RSV_BOOKED_MIN.get(code or "", 120)
        return start, start + timedelta(minutes=occ)
    if kind == "bar":
        occ = RSV_BAR_OCCUPY_MIN
    else:
        occ = RSV_DUR_OCCUPY_MIN.get(code or "", RSV_DUR_OCCUPY_MIN["2-3"])
    close = _rsv_close_dt(date_s)
    end = close if occ is None else min(close, start + timedelta(minutes=occ))
    return start, end


def rsv_fits_closing(kind: str, date_s: str, time_s: str, code: str) -> bool:
    """Помещается ли вариант до закрытия в 03:00."""
    try:
        start = _arrival(date_s, time_s)
    except ValueError:
        return False
    return start + timedelta(minutes=_rsv_need_min(kind, code)) <= _rsv_close_dt(date_s)


def db_rsv_active_rows(kind: str, date_s: str, exclude_id: int | None = None) -> list[dict]:
    """Активные брони категории на рабочий день («Ожидает»/«Подтверждена»)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, kind, date, time, duration, actual_min FROM reservations"
            " WHERE kind = ? AND date = ? AND status IN ('new', 'confirmed') AND id != ?",
            (kind, date_s, int(exclude_id or 0)),
        ).fetchall()
    return [dict(r) for r in rows]


def _rsv_free_in(rows: list[dict], kind: str, date_s: str, time_s: str, code: str,
                 actual_min: int | None = None) -> bool:
    """True — в интервале новой брони одновременно занято меньше RSV_CAPACITY
    мест. Для VIP к концу каждой брони добавляется время на уборку."""
    cap = RSV_CAPACITY.get(kind)
    if cap is None:
        return True
    buf = timedelta(minutes=RSV_BUFFER_MIN.get(kind, 0))
    s0, e0 = rsv_interval(kind, date_s, time_s, code, actual_min)
    e0 += buf
    ivs: list[tuple[datetime, datetime]] = []
    for r in rows:
        try:
            rs, r_end = rsv_interval(r["kind"], r["date"], r["time"],
                                     r.get("duration") or "", r.get("actual_min"))
        except (ValueError, TypeError, KeyError):
            continue
        r_end += buf
        if rs < e0 and s0 < r_end:
            ivs.append((rs, r_end))
    if len(ivs) < cap:
        return True
    # максимум одновременных броней достигается в начале новой или чьей-то брони
    for p in [s0] + [rs for rs, _ in ivs if rs > s0]:
        if sum(1 for rs, r_end in ivs if rs <= p < r_end) >= cap:
            return False
    return True


def rsv_options(kind: str, date_s: str, time_s: str, rows: list[dict] | None = None) -> list[str]:
    """Доступные варианты длительности на это время (у бара — [""])."""
    if rows is None:
        rows = db_rsv_active_rows(kind, date_s)
    codes = [""] if kind == "bar" else RSV_DUR_BY_KIND.get(kind, [])
    return [
        c for c in codes
        if rsv_fits_closing(kind, date_s, time_s, c)
        and _rsv_free_in(rows, kind, date_s, time_s, c)
    ]


def rsv_slots_available(kind: str, date_s: str) -> list[str]:
    """Время, на которое ещё можно забронировать: не прошло, помещается до
    закрытия и есть свободное место хотя бы для одного варианта длительности."""
    try:
        datetime.strptime(date_s, "%Y-%m-%d")
    except ValueError:
        return []
    now = _now_dt() + timedelta(minutes=RSV_LEAD_MIN)
    rows = db_rsv_active_rows(kind, date_s)
    return [
        sl for sl in _slots()
        if _arrival(date_s, sl) > now and rsv_options(kind, date_s, sl, rows)
    ]


def rsv_capacity_full(kind: str, date_s: str) -> bool:
    """True — на эту дату для категории не осталось ни одного доступного времени."""
    return not rsv_slots_available(kind, date_s)


def db_upcoming_reservations(limit: int = 10) -> list[dict]:
    today = _biz_today().strftime("%Y-%m-%d")  # до 03:00 идёт вчерашняя смена
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reservations WHERE date >= ? ORDER BY date, time, id LIMIT ?",
            (today, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def db_reservations_for_date(date_s: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM reservations"
            " WHERE date = ? AND status != 'cancelled'",
            (date_s,),
        ).fetchone()
    return row["cnt"]


# ================================== МЕНЮ ====================================

MENU_PATH = Path(__file__).with_name("menu.json")

_DEFAULT_MENU = {
    "categories": [
        {
            "id": "hookah",
            "emoji": "💨",
            "name": "Кальяны",
            "items": [
                {"id": "h1", "name": "Классический", "price": 1500},
                {"id": "h2", "name": "Фруктовый микс", "price": 1700},
                {"id": "h3", "name": "Крепкий", "price": 1500},
                {"id": "h4", "name": "Авторский", "price": 2000},
            ],
        },
        {
            "id": "drinks",
            "emoji": "🥤",
            "name": "Напитки",
            "items": [
                {"id": "d1", "name": "Кола / Спрайт", "price": 250},
                {"id": "d2", "name": "Свежевыжатый сок", "price": 450},
                {"id": "d3", "name": "Мохито", "price": 400},
                {"id": "d4", "name": "Вода", "price": 150},
            ],
        },
        {
            "id": "food",
            "emoji": "🍗",
            "name": "Закуски",
            "items": [
                {"id": "f1", "name": "Крылья BBQ", "price": 450},
                {"id": "f2", "name": "Картофель фри", "price": 300},
                {"id": "f3", "name": "Сырные палочки", "price": 390},
                {"id": "f4", "name": "Сет «Пятих», 5 шт.", "price": 690},
            ],
        },
    ]
}


def _load_menu() -> dict:
    try:
        with MENU_PATH.open(encoding="utf-8") as f:
            m = json.load(f)
        if m.get("categories"):
            return m
        logger.warning("menu.json пуст — использую встроенное демо-меню")
    except FileNotFoundError:
        logger.warning("menu.json не найден рядом с bot.py — использую демо-меню")
    except json.JSONDecodeError:
        logger.warning("menu.json с ошибкой синтаксиса — использую демо-меню")
    return _DEFAULT_MENU


MENU = _load_menu()


def seed_stock_from_menu() -> None:
    """Строки склада для всех позиций меню: позиция = ингредиент (единица «шт»),
    рецепт 1 порция = 1 шт. Идемпотентно — существующие рецепты и одноимённые
    позиции не трогаем. qty=999 — заглушка, замените реальным остатком."""
    created_ings = created_recipes = 0
    now = _now()
    with _connect() as conn:
        for cat in MENU.get("categories", []):
            for it in cat.get("items", []):
                has = conn.execute(
                    "SELECT 1 FROM recipes WHERE item_id = ? LIMIT 1", (it["id"],)
                ).fetchone()
                if has:
                    continue  # рецепт уже настроен — не вмешиваемся
                row = conn.execute(
                    "SELECT id FROM ingredients WHERE name = ?", (it["name"],)
                ).fetchone()
                if row:
                    ing_id = int(row["id"])
                else:
                    cur = conn.execute(
                        "INSERT INTO ingredients"
                        " (name, unit, qty, cost, min_qty, created_at, updated_at)"
                        " VALUES (?, 'шт', 999, 0, 0, ?, ?)",
                        (it["name"], now, now),
                    )
                    ing_id = int(cur.lastrowid)
                    created_ings += 1
                conn.execute(
                    "INSERT OR IGNORE INTO recipes (item_id, ingredient_id, qty)"
                    " VALUES (?, ?, 1)",
                    (it["id"], ing_id),
                )
                created_recipes += 1
    if created_ings or created_recipes:
        logger.info(
            "Склад пополнен из меню: +%d позиций, +%d рецептов",
            created_ings, created_recipes,
        )


def esc(text) -> str:
    """Экранирование для HTML-разметки Telegram."""
    return html.escape(str(text))


def f_category(cat_id: str) -> dict | None:
    return next((c for c in MENU["categories"] if c["id"] == cat_id), None)


def f_find_item(item_id: str) -> dict | None:
    for c in MENU["categories"]:
        for it in c["items"]:
            if it["id"] == item_id:
                return it
    return None


# ============================ АКЦИИ («Выгода») ===============================
# Заказ по акции — ОТДЕЛЬНЫЙ заказ (своя корзина). Баллы на него не начисляются
# и не списываются («акции не суммируются»). Склад списывается по рецептам
# обычных позиций меню, из которых состоит акция (comps).
HH_WEEKDAYS = (0, 1, 2, 3, 4)   # пн–пт
HH_START_H, HH_END_H = 15, 18   # счастливые часы 15:00–18:00 (МСК)
HH_GRACE_MIN = 10               # оформить набранное можно до 18:10
# «Любой кальян по полной цене + чай в подарок» на афише входит в счастливые
# часы. True — разрешить подарочный чай в любое время.
PROMO_TEA_ALWAYS = False
PROMO_TEA_HOOKAH_CATS = ("hookah", "hookah_huka")         # какие кальяны «любые»
PROMO_TEA_CATS = ("tea_green", "tea_black", "tea_karkade")  # какой чай в подарок
PROMO_ITEMS: dict[str, dict] = {
    "pr_hh1": {"name": "☀️ Классический кальян (счастливые часы)",
               "price": 1300, "comps": [("h1", 1)], "hh": True},
    "pr_hh2": {"name": "☀️ Премиальный кальян (счастливые часы)",
               "price": 1600, "comps": [("h2", 1)], "hh": True},
    "pr_cb1": {"name": "🔥 Комбо: 2 классических кальяна на ХУКА Про",
               "price": 2500, "comps": [("hu1", 2)], "hh": False},
    "pr_cb2": {"name": "🔥 Комбо: классический + премиальный на ХУКА Про",
               "price": 2800, "comps": [("hu1", 1), ("hu2", 1)], "hh": False},
    "pr_cb3": {"name": "🔥 Комбо: 2 премиальных кальяна на ХУКА Про",
               "price": 3000, "comps": [("hu2", 2)], "hh": False},
}


def _is_promo_id(item_id: str | None) -> bool:
    return bool(item_id) and str(item_id).startswith("pr_")


def _items_in_cats(cat_ids) -> list[dict]:
    out: list[dict] = []
    for cid in cat_ids:
        cat = f_category(cid)
        if cat:
            out.extend(cat.get("items", []))
    return out


def promo_item(pid: str) -> dict | None:
    """Описание акционной позиции (id, name, price, comps, hh, old) или None,
    если такой акции нет / нужных позиций нет в меню."""
    if pid in PROMO_ITEMS:
        p = PROMO_ITEMS[pid]
        old = 0
        for base_id, k in p["comps"]:
            base = f_find_item(base_id)
            if not base:
                return None
            old += int(base["price"]) * k
        return {"id": pid, "name": p["name"], "price": int(p["price"]),
                "comps": list(p["comps"]), "hh": bool(p["hh"]), "old": old}
    if pid.startswith("pr_t:"):  # pr_t:<кальян>:<чай>
        parts = pid.split(":")
        if len(parts) != 3:
            return None
        hid, tid = parts[1], parts[2]
        hookah = next((i for i in _items_in_cats(PROMO_TEA_HOOKAH_CATS) if i["id"] == hid), None)
        tea = next((i for i in _items_in_cats(PROMO_TEA_CATS) if i["id"] == tid), None)
        if not hookah or not tea:
            return None
        return {"id": pid, "name": f"🍵 {hookah['name']} + чай «{tea['name']}» в подарок",
                "price": int(hookah["price"]), "comps": [(hid, 1), (tid, 1)],
                "hh": not PROMO_TEA_ALWAYS,
                "old": int(hookah["price"]) + int(tea["price"])}
    return None


def hh_active(grace_min: int = 0) -> bool:
    """Идут ли счастливые часы (будни 15:00–18:00 МСК; grace — минуты после 18:00)."""
    now = _now_dt()
    if now.weekday() not in HH_WEEKDAYS:
        return False
    start = now.replace(hour=HH_START_H, minute=0, second=0, microsecond=0)
    end = now.replace(hour=HH_END_H, minute=0, second=0, microsecond=0)
    return start <= now < end + timedelta(minutes=grace_min)


def _components(item_id: str) -> list[tuple[str, int]]:
    """Из каких позиций меню состоит позиция заказа (для склада)."""
    if _is_promo_id(item_id):
        p = promo_item(item_id)
        return list(p["comps"]) if p else []
    return [(item_id, 1)]


# ================================ ТЕКСТЫ ====================================

STATUSES: dict[str, str] = {
    "accepted": "🟡 Принят",
    "ready": "🟢 Готов",
    "issued": "🔵 Выдан",
    "paid": "💵 Оплачен",
    "cancelled": "❌ Отменён",
}

# ------------------------------------------------------ СКЛАД ----------------
def _fmt_qty(n: float) -> str:
    """85.0 → «85», 10.5 → «10.5»."""
    return f"{float(n):g}"


def _fmt_price(n) -> str:
    """Себестоимость: 650 → «650 ₽», 6.5 → «6,5 ₽»."""
    v = float(n)
    if v == int(v):
        return _fmt_money(int(v))
    return f"{v:g}".replace(".", ",") + f" {CURRENCY}"


def db_list_ingredients() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM ingredients ORDER BY name")]


def db_get_ingredient(ing_id: int) -> dict | None:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM ingredients WHERE id = ?", (ing_id,)).fetchone()
        return dict(r) if r else None


def db_add_ingredient(name: str, unit: str, qty: float, cost: float, min_qty: float) -> int:
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO ingredients (name, unit, qty, cost, min_qty, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name.strip(), unit, float(qty), float(cost), float(min_qty), now, now),
        )
        return int(cur.lastrowid)


def db_set_stock_unit(ing_id: int, unit: str) -> None:
    """Смена единицы измерения позиции (шт / г / мл)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE ingredients SET unit = ?, updated_at = ? WHERE id = ?",
            (unit, _now(), int(ing_id)),
        )


def stock_move(ing_id: int, delta: float, reason: str, ref: str | None = None) -> dict | None:
    """Меняет остаток и пишет движение. Возвращает новое состояние или None."""
    with _connect() as conn:
        r = conn.execute("SELECT * FROM ingredients WHERE id = ?", (ing_id,)).fetchone()
        if not r:
            return None
        new_qty = float(r["qty"]) + float(delta)
        conn.execute(
            "UPDATE ingredients SET qty = ?, updated_at = ? WHERE id = ?",
            (new_qty, _now(), ing_id),
        )
        conn.execute(
            "INSERT INTO stock_moves (ingredient_id, delta, reason, ref, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (ing_id, float(delta), reason, ref, _now()),
        )
        d = dict(r)
        d["qty"] = new_qty
        return d


def db_set_cost(ing_id: int, cost: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE ingredients SET cost = ?, updated_at = ? WHERE id = ?",
            (float(cost), _now(), ing_id),
        )


def db_set_min_qty(ing_id: int, min_qty: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE ingredients SET min_qty = ?, updated_at = ? WHERE id = ?",
            (float(min_qty), _now(), ing_id),
        )


def db_delete_ingredient(ing_id: int) -> None:
    """Удаление позиции: из склада, её рецепты и журнал движений."""
    with _connect() as conn:
        conn.execute("DELETE FROM recipes WHERE ingredient_id = ?", (ing_id,))
        conn.execute("DELETE FROM stock_moves WHERE ingredient_id = ?", (ing_id,))
        conn.execute("DELETE FROM ingredients WHERE id = ?", (ing_id,))


def db_recent_moves(ing_id: int, limit: int = 5) -> list[dict]:
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM stock_moves WHERE ingredient_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (ing_id, limit),
            )
        ]


def db_recipes_for_item(item_id: str) -> list[dict]:
    """Ингредиенты позиции меню с количеством на 1 порцию."""
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT r.id AS rid, r.qty AS need, i.* FROM recipes r"
                " JOIN ingredients i ON i.id = r.ingredient_id"
                " WHERE r.item_id = ? ORDER BY i.name",
                (item_id,),
            )
        ]


def db_upsert_recipe(item_id: str, ing_id: int, qty: float) -> bool:
    """True — добавлено, False — уже было (обновили количество)."""
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO recipes (item_id, ingredient_id, qty) VALUES (?, ?, ?)",
                (item_id, ing_id, float(qty)),
            )
            return True
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE recipes SET qty = ? WHERE item_id = ? AND ingredient_id = ?",
                (float(qty), item_id, ing_id),
            )
            return False


def db_delete_recipe(item_id: str, ing_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM recipes WHERE item_id = ? AND ingredient_id = ?",
            (item_id, ing_id),
        )


def _item_needs(conn: sqlite3.Connection, item_id: str) -> dict[int, float]:
    """Сколько каждого ингредиента уходит на 1 шт позиции заказа
    (акции раскладываются на обычные позиции меню)."""
    needs: dict[int, float] = {}
    for base_id, k in _components(item_id):
        for r in conn.execute(
            "SELECT r.ingredient_id, r.qty FROM recipes r"
            " JOIN ingredients i ON i.id = r.ingredient_id WHERE r.item_id = ?",
            (base_id,),
        ):
            iid = int(r["ingredient_id"])
            needs[iid] = needs.get(iid, 0.0) + float(r["qty"]) * k
    return needs


def _reserved_map(conn: sqlite3.Connection) -> dict[int, float]:
    """Ингредиенты, «зарезервированные» открытыми (ещё не оплаченными и не
    отменёнными) заказами — склад списывается только при оплате, поэтому без
    резерва несколько открытых заказов могли бы продать больше, чем есть.
    Заказы старше RESERVE_HOURS (забыли закрыть) резерв не держат; предзаказ
    держит резерв, пока время прихода не ушло в прошлое больше чем на RESERVE_HOURS."""
    since = (_now_dt() - timedelta(hours=RESERVE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT oi.item_id, oi.qty FROM order_items oi JOIN orders o ON o.id = oi.order_id"
        " WHERE o.status IN ('accepted', 'ready', 'issued') AND o.stock_deducted = 0"
        " AND oi.item_id IS NOT NULL AND (o.created_at >= ? OR o.arrive_at >= ?)",
        (since, since),
    ).fetchall()
    cache: dict[str, dict[int, float]] = {}
    res: dict[int, float] = {}
    for it in rows:
        iid = it["item_id"]
        if iid not in cache:
            cache[iid] = _item_needs(conn, iid)
        for ing_id, need in cache[iid].items():
            res[ing_id] = res.get(ing_id, 0.0) + need * int(it["qty"])
    return res


def reserved_snapshot() -> dict[int, float]:
    with _connect() as conn:
        return _reserved_map(conn)


def max_available(item_id: str, reserved: dict[int, float] | None = None) -> int | None:
    """Сколько штук позиции можно продать по остаткам за вычетом резерва
    открытых заказов. None — рецепта нет, ограничения нет."""
    with _connect() as conn:
        needs = _item_needs(conn, item_id)
        if not needs:
            return None
        if reserved is None:
            reserved = _reserved_map(conn)
        limit: int | None = None
        for ing_id, need in needs.items():
            if need <= 0:
                continue
            row = conn.execute("SELECT qty FROM ingredients WHERE id = ?", (ing_id,)).fetchone()
            if not row:
                continue
            free = max(0.0, float(row["qty"]) - reserved.get(ing_id, 0.0))
            avail = int((free + 1e-9) // need)
            limit = avail if limit is None else min(limit, avail)
        return limit


def item_in_stock(item_id: str, reserved: dict[int, float] | None = None) -> bool:
    """False — позицию скрыть из меню (не хватает ингредиентов даже на 1 шт).
    Нет рецепта — True (не списываем — значит, не блокируем)."""
    m = max_available(item_id, reserved)
    return m is None or m >= 1


def cart_stock_problems(cart: list[dict]) -> list[str]:
    """Нехватка ингредиентов под ВСЮ корзину (с учётом общих ингредиентов
    и резерва открытых заказов)."""
    problems: list[str] = []
    with _connect() as conn:
        demands: dict[int, float] = {}
        for it in cart:
            iid = it.get("id")
            if not iid:
                continue
            for ing_id, need in _item_needs(conn, iid).items():
                demands[ing_id] = demands.get(ing_id, 0.0) + need * int(it["qty"])
        reserved = _reserved_map(conn)
        for ing_id, need in demands.items():
            row = conn.execute(
                "SELECT * FROM ingredients WHERE id = ?", (ing_id,)
            ).fetchone()
            if not row:
                continue
            free = max(0.0, float(row["qty"]) - reserved.get(ing_id, 0.0))
            if need > free + 1e-9:
                problems.append(
                    f"• {esc(row['name'])} — нужно {_fmt_qty(need)} {esc(row['unit'])}, "
                    f"а доступно {_fmt_qty(free)} {esc(row['unit'])}"
                )
    return problems


def stock_apply_order(order: dict, sign: int) -> list[dict]:
    """Списание (sign=-1) или возврат (sign=+1) ингредиентов по рецепту позиций
    заказа. Возвращает затронутые ингредиенты с новыми остатками."""
    with _connect() as conn:
        items = conn.execute(
            "SELECT item_id, qty FROM order_items"
            " WHERE order_id = ? AND item_id IS NOT NULL",
            (order["id"],),
        ).fetchall()
        affected: dict[int, dict] = {}
        for it in items:
            for ing_id, need in _item_needs(conn, it["item_id"]).items():
                delta = float(sign) * float(need) * float(it["qty"])
                row = conn.execute(
                    "SELECT * FROM ingredients WHERE id = ?", (ing_id,)
                ).fetchone()
                if not row:
                    continue
                new_qty = float(row["qty"]) + delta
                conn.execute(
                    "UPDATE ingredients SET qty = ?, updated_at = ? WHERE id = ?",
                    (new_qty, _now(), row["id"]),
                )
                conn.execute(
                    "INSERT INTO stock_moves (ingredient_id, delta, reason, ref,"
                    " created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        row["id"],
                        delta,
                        "списание" if sign < 0 else "возврат",
                        f"заказ#{order['id']}",
                        _now(),
                    ),
                )
                d = dict(row)
                d["qty"] = new_qty
                affected[row["id"]] = d
        return list(affected.values())


def db_stock_value() -> int:
    """Вложено денег в остатки."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT COALESCE(SUM(qty * cost), 0) AS v FROM ingredients"
        ).fetchone()
        return int(round(r["v"]))


def db_cogs_range(d_from: str, d_to: str) -> int:
    """Себестоимость проданного за период (по текущим с/с ингредиентов):
    только ОПЛАЧЕННЫЕ заказы, по рецептам их позиций (акции — по составу)."""
    total = 0.0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT oi.item_id, oi.qty FROM order_items oi"
            " JOIN orders o ON o.id = oi.order_id"
            " WHERE substr(o.created_at, 1, 10) BETWEEN ? AND ?"
            " AND o.status = 'paid' AND oi.item_id IS NOT NULL",
            (d_from, d_to),
        ).fetchall()
        costs = {int(r["id"]): float(r["cost"]) for r in conn.execute("SELECT id, cost FROM ingredients")}
        for it in rows:
            for ing_id, need in _item_needs(conn, it["item_id"]).items():
                total += need * float(it["qty"]) * costs.get(ing_id, 0.0)
    return int(round(total))


def db_cogs_today(date_s: str) -> int:
    return db_cogs_range(date_s, date_s)


async def _stock_alerts(bot, affected: list[dict]) -> None:
    """Личное сообщение админам: закончилось или упало ниже минимума."""
    lines: list[str] = []
    for ing in affected:
        q, mn = float(ing["qty"]), float(ing["min_qty"])
        if q <= 0:
            lines.append(
                f"⛔ Закончилось: «{esc(ing['name'])}» (0 {esc(ing['unit'])}) — "
                "позиции меню с ним скрыты до прихода"
            )
        elif mn > 0 and q <= mn:
            lines.append(
                f"⚠️ Мало: «{esc(ing['name'])}» — {_fmt_qty(q)} {esc(ing['unit'])} "
                f"(мин {_fmt_qty(mn)}), пора заказать"
            )
    if not lines:
        return
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "📦 <b>Склад</b>\n" + "\n".join(lines))
        except Exception:
            logger.info("Не удалось прислать склад-алерт админу %s", admin_id)


# ============================== ЛОЯЛЬНОСТЬ ==================================
# Карта гостя = его Telegram-аккаунт + номер телефона (номер карты).
# Найти гостя админ может по телефону или @нику. Баллы: 1 балл = 1 ₽,
# кешбэк LOYALTY_PERCENT % от оплаченной деньгами части обычного заказа.
# Баланс всегда = сумма журнала bonus_moves (никаких «кэшированных» остатков).


def normalize_phone(raw) -> str | None:
    """«8 (900) 123-45-67» / «9001234567» / «+7 900…» → «+79001234567»."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits  # российский мобильный без кода страны
    if not 10 <= len(digits) <= 15:
        return None
    return "+" + digits


def db_guest_get(user_id: int | None) -> dict | None:
    if not user_id:
        return None
    with _connect() as conn:
        row = conn.execute("SELECT * FROM guests WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def guest_card(user_id: int | None) -> dict | None:
    """Карта есть, только если гость оставил телефон."""
    g = db_guest_get(user_id)
    return g if g and g.get("phone") else None


def db_guest_touch(user) -> None:
    """Обновить @ник и имя у существующей карты (для поиска по нику)."""
    if not user:
        return
    with _connect() as conn:
        conn.execute(
            "UPDATE guests SET username = ?, first_name = ?, updated_at = ? WHERE user_id = ?",
            ((user.username or None), (user.first_name or None), _now(), user.id),
        )


def db_guest_set_phone(user, phone: str, verified: bool) -> tuple[bool, str]:
    """Привязать телефон к карте гостя. (True, "") — ок; (False, "taken") —
    номер уже принадлежит другому аккаунту. Подтверждённый (своим контактом)
    номер забирает себе номер, введённый кем-то вручную без подтверждения."""
    now = _now()
    with _connect() as conn:
        owner = conn.execute(
            "SELECT user_id, phone_verified FROM guests WHERE phone = ?", (phone,)
        ).fetchone()
        if owner and int(owner["user_id"]) != int(user.id):
            if verified and not owner["phone_verified"]:
                conn.execute(
                    "UPDATE guests SET phone = NULL, phone_verified = 0, updated_at = ?"
                    " WHERE user_id = ?",
                    (now, owner["user_id"]),
                )
                logger.warning(
                    "Телефон %s перепривязан: был у %s (не подтверждён) → %s (контакт)",
                    phone, owner["user_id"], user.id,
                )
            else:
                return False, "taken"
        me = conn.execute(
            "SELECT phone, phone_verified FROM guests WHERE user_id = ?", (user.id,)
        ).fetchone()
        if me:
            keep_verified = bool(me["phone_verified"]) and me["phone"] == phone
            conn.execute(
                "UPDATE guests SET phone = ?, phone_verified = ?, username = ?,"
                " first_name = ?, updated_at = ? WHERE user_id = ?",
                (phone, int(verified or keep_verified), user.username or None,
                 user.first_name or None, now, user.id),
            )
        else:
            conn.execute(
                "INSERT INTO guests (user_id, phone, phone_verified, username, first_name,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user.id, phone, int(verified), user.username or None,
                 user.first_name or None, now, now),
            )
    return True, ""


def _bonus_balance(conn: sqlite3.Connection, user_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(delta), 0) AS b FROM bonus_moves WHERE user_id = ?", (user_id,)
    ).fetchone()
    return int(row["b"])


def db_bonus_balance(user_id: int | None) -> int:
    if not user_id:
        return 0
    with _connect() as conn:
        return _bonus_balance(conn, user_id)


def _bonus_add(conn, user_id: int, delta: int, kind: str, amount: int | None = None,
               order_id: int | None = None, admin_id: int | None = None,
               note: str | None = None) -> None:
    conn.execute(
        "INSERT INTO bonus_moves (user_id, delta, kind, amount, order_id, admin_id, note,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, int(delta), kind, amount, order_id, admin_id, note, _now()),
    )


def bonus_max_for(total: int) -> int:
    """Сколько баллов можно списать в оплату чека на сумму total."""
    return max(0, int(total) * BONUS_MAX_PAY_PCT // 100)


def bonus_available_for_order(order: dict) -> int:
    """Сколько баллов можно списать в этот заказ (0 — нельзя: акция/нет карты)."""
    if order.get("kind") == "promo" or not order.get("user_id"):
        return 0
    if not guest_card(order["user_id"]):
        return 0
    return min(db_bonus_balance(order["user_id"]), bonus_max_for(order["total"]))


def db_set_bonus_pending(order_id: int, points: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE orders SET bonus_pending = ? WHERE id = ? AND status != 'paid'",
            (max(0, int(points)), order_id),
        )


def db_claim_paid(order_id: int, method: str) -> bool:
    """Атомарно перевести заказ в «Оплачен». True — перевели мы (дальше —
    баллы и склад); False — заказ уже был оплачен (двойное нажатие)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE orders SET status = 'paid', pay_method = ?, updated_at = ?"
            " WHERE id = ? AND status != 'paid'",
            (method, _now(), order_id),
        )
        return cur.rowcount == 1


def loyalty_on_paid(order_id: int) -> dict:
    """После оплаты: списать «отложенные» баллы и начислить кешбэк.
    Вызывать только тому, кто выиграл db_claim_paid (ровно один раз)."""
    res = {"spent": 0, "accrued": 0, "balance": None}
    with _connect() as conn:
        o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not o:
            return res
        uid = o["user_id"]
        card = None
        if uid and o["kind"] != "promo":
            card = conn.execute(
                "SELECT 1 FROM guests WHERE user_id = ? AND phone IS NOT NULL", (uid,)
            ).fetchone()
        if not card:
            conn.execute("UPDATE orders SET bonus_pending = 0 WHERE id = ?", (order_id,))
            return res
        bal = _bonus_balance(conn, uid)
        spend = max(0, min(int(o["bonus_pending"] or 0), bal, bonus_max_for(o["total"])))
        if spend:
            _bonus_add(conn, uid, -spend, "spend", order_id=order_id,
                       note=f"оплата заказа #{order_id}")
        accrue = (int(o["total"]) - spend) * LOYALTY_PERCENT // 100
        if accrue > 0:
            _bonus_add(conn, uid, accrue, "accrual", amount=int(o["total"]) - spend,
                       order_id=order_id, note=f"кешбэк {LOYALTY_PERCENT}% за заказ #{order_id}")
        conn.execute(
            "UPDATE orders SET bonus_used = ?, bonus_accrued = ?, bonus_pending = 0 WHERE id = ?",
            (spend, max(0, accrue), order_id),
        )
        res.update(spent=spend, accrued=max(0, accrue), balance=_bonus_balance(conn, uid))
    return res


def loyalty_on_refund(order_id: int) -> dict:
    """Возврат оплаченного заказа: вернуть списанные баллы и забрать
    начисленный кешбэк (не ниже нуля). Атомарно и ровно один раз."""
    res = {"returned": 0, "taken": 0, "balance": None, "user_id": None}
    with _connect() as conn:
        o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not o or not o["user_id"]:
            return res
        cur = conn.execute(
            "UPDATE orders SET bonus_used = 0, bonus_accrued = 0, bonus_pending = 0"
            " WHERE id = ? AND (bonus_used > 0 OR bonus_accrued > 0)",
            (order_id,),
        )
        if cur.rowcount != 1:
            return res
        uid = int(o["user_id"])
        used, acc = int(o["bonus_used"] or 0), int(o["bonus_accrued"] or 0)
        if used:
            _bonus_add(conn, uid, used, "refund", order_id=order_id,
                       note=f"возврат баллов, заказ #{order_id}")
        take = 0
        if acc:
            take = min(acc, max(0, _bonus_balance(conn, uid)))
            if take:
                note = f"отмена кешбэка, заказ #{order_id}"
                if take < acc:
                    note += f" (не хватило {acc - take})"
                _bonus_add(conn, uid, -take, "refund", order_id=order_id, note=note)
        res.update(returned=used, taken=take, balance=_bonus_balance(conn, uid), user_id=uid)
    return res


def loyalty_offline_accrue(user_id: int, check_sum: int, admin_id: int) -> int:
    """Админ начисляет кешбэк за чек, оплаченный мимо бота."""
    pts = int(check_sum) * LOYALTY_PERCENT // 100
    with _connect() as conn:
        _bonus_add(conn, user_id, pts, "offline", amount=int(check_sum),
                   admin_id=admin_id, note=f"чек {int(check_sum)} ₽ (вне бота)")
    return pts


def loyalty_manual_spend(user_id: int, points: int, admin_id: int) -> bool:
    with _connect() as conn:
        if points <= 0 or _bonus_balance(conn, user_id) < points:
            return False
        _bonus_add(conn, user_id, -int(points), "manual", admin_id=admin_id,
                   note="списание администратором")
    return True


def guest_stats(user_id: int) -> dict:
    """Сумма оплат, число заказов, визиты (дни), последний визит, история."""
    with _connect() as conn:
        o = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total - bonus_used), 0) AS s"
            " FROM orders WHERE user_id = ? AND status = 'paid'",
            (user_id,),
        ).fetchone()
        off = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM bonus_moves"
            " WHERE user_id = ? AND kind = 'offline'",
            (user_id,),
        ).fetchone()
        rent = conn.execute(
            "SELECT COALESCE(SUM(price), 0) AS s FROM reservations"
            " WHERE user_id = ? AND status = 'paid'",
            (user_id,),
        ).fetchone()
        # рабочий день 15:00–03:00: заказ в 01:00 относится к предыдущей дате
        days = conn.execute(
            "SELECT COUNT(DISTINCT d) AS n, MAX(d) AS last FROM ("
            " SELECT date(created_at, '-6 hours') AS d FROM orders"
            "  WHERE user_id = ? AND status = 'paid'"
            " UNION SELECT date(created_at, '-6 hours') FROM bonus_moves"
            "  WHERE user_id = ? AND kind = 'offline'"
            " UNION SELECT date FROM reservations"
            "  WHERE user_id = ? AND status = 'paid')",
            (user_id, user_id, user_id),
        ).fetchone()
        moves = [
            dict(r) for r in conn.execute(
                "SELECT * FROM bonus_moves WHERE user_id = ? ORDER BY id DESC LIMIT 7",
                (user_id,),
            )
        ]
        bal = _bonus_balance(conn, user_id)
    return {
        "orders": int(o["n"]), "spent": int(o["s"]) + int(off["s"]) + int(rent["s"]),
        "visits": int(days["n"] or 0), "last": days["last"], "balance": bal, "moves": moves,
    }


def db_guest_search(query: str) -> list[dict]:
    """Поиск карты по телефону (полностью или последние 10 цифр) или @нику."""
    q = (query or "").strip()
    with _connect() as conn:
        if q.startswith("@") or re.search(r"[A-Za-z_]", q):
            nick = q.lstrip("@").lower()
            if not nick:
                return []
            rows = conn.execute(
                "SELECT * FROM guests WHERE phone IS NOT NULL AND lower(username) = ?",
                (nick,),
            ).fetchall()
            return [dict(r) for r in rows]
        digits = re.sub(r"\D", "", q)
        if len(digits) < 4:
            return []
        norm = normalize_phone(q)
        rows = []
        if norm:
            rows = conn.execute("SELECT * FROM guests WHERE phone = ?", (norm,)).fetchall()
        if not rows:
            tail = digits[-10:]
            rows = conn.execute(
                "SELECT * FROM guests WHERE phone IS NOT NULL AND phone LIKE ? LIMIT 10",
                ("%" + tail,),
            ).fetchall()
    return [dict(r) for r in rows]


BONUS_KIND_LABELS = {
    "accrual": "кешбэк", "spend": "оплата баллами", "refund": "возврат",
    "offline": "чек вне бота", "manual": "списание админом",
}


def _fmt_pts(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def card_text(user_id: int, for_admin: bool = False) -> str:
    g = db_guest_get(user_id) or {}
    st = guest_stats(user_id)
    lines = ["💎 <b>Карта лояльности Zig Zag</b>", ""]
    if for_admin:
        who = esc(g.get("first_name") or "Гость")
        if g.get("username"):
            who += f" (@{esc(g['username'])})"
        lines.append(f"👤 {who} · id <code>{user_id}</code>")
    phone = g.get("phone") or "—"
    mark = " ✅" if g.get("phone_verified") else (" (введён вручную)" if g.get("phone") else "")
    lines += [
        f"📱 Номер карты: <b>{esc(phone)}</b>{mark}",
        f"🎁 Баллы: <b>{_fmt_pts(st['balance'])}</b> (1 балл = 1 {CURRENCY})",
        f"💸 Кешбэк: <b>{LOYALTY_PERCENT}%</b> с оплаченных заказов"
        f" · оплатить баллами можно до {BONUS_MAX_PAY_PCT}% чека",
        f"🧾 Оплачено всего: {_fmt_money(st['spent'])} · заказов: {st['orders']}"
        f" · визитов: {st['visits']}",
    ]
    if st["last"]:
        lines.append(f"🕒 Последний визит: {st['last'][8:10]}.{st['last'][5:7]}.{st['last'][:4]}")
    if st["moves"]:
        lines += ["", "<b>Последние операции:</b>"]
        for m in st["moves"]:
            d = int(m["delta"])
            sign = "+" if d >= 0 else "−"
            when = (m.get("created_at") or "")[5:16]
            label = BONUS_KIND_LABELS.get(m["kind"], m["kind"])
            ref = f" · #{m['order_id']}" if m.get("order_id") else ""
            lines.append(f"  {sign}{_fmt_pts(abs(d))} · {label}{ref} · {when}")
    lines += ["", "ℹ️ На заказы по акциям («Выгода») баллы не начисляются и не списываются."]
    return "\n".join(lines)


# ========================= АНАЛИТИКА ПО КАРТАМ ЛОЯЛЬНОСТИ ======================
# Деньги гостя = заказы в боте (оплачено деньгами, без баллов) + чеки вне бота
# (введённые через /guest) + оплаченная аренда VIP/PlayStation.
# Визит = рабочий день (15:00–03:00), в который была оплата.
SEG_SLEEP_DAYS = 30        # «😴 давно не был» — дней без визита
SEG_VIP_SPENT = 50000      # «💎 ценный гость» — потратил от, ₽
SEG_VIP_VISITS = 10        # …или визитов от
SEG_OFTEN_30 = 3           # «🔥 ходит часто» — визитов за последние 30 дней от
_MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
_TIME_BUCKETS = [(15, 18, "15–18"), (18, 21, "18–21"), (21, 24, "21–00"), (0, 3, "00–03")]


def _an_today():
    return (_now_dt() - timedelta(hours=6)).date()


def _an_day(ts: str):
    """Рабочий день оплаты: 01:00 ночи — это ещё предыдущая дата."""
    return (datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S") - timedelta(hours=6)).date()


def _an_events(conn: sqlite3.Connection, uid: int | None = None) -> list[dict]:
    """Все оплаты гостей: заказы, чеки вне бота, аренда. uid=None — всех."""
    flt, args = ("AND user_id = ?", (uid,)) if uid else ("AND user_id IS NOT NULL", ())
    ev: list[dict] = []
    for r in conn.execute(
        f"SELECT id, user_id, total - bonus_used AS m, kind, pay_method, created_at"
        f" FROM orders WHERE status = 'paid' {flt}", args,
    ):
        ev.append({"uid": r["user_id"], "src": "promo" if r["kind"] == "promo" else "order",
                   "m": int(r["m"] or 0), "day": _an_day(r["created_at"]),
                   "hour": int(str(r["created_at"])[11:13]), "pay": r["pay_method"]})
    for r in conn.execute(
        f"SELECT user_id, amount, created_at FROM bonus_moves WHERE kind = 'offline' {flt}", args
    ):
        ev.append({"uid": r["user_id"], "src": "offline", "m": int(r["amount"] or 0),
                   "day": _an_day(r["created_at"]), "hour": None, "pay": None})
    for r in conn.execute(
        f"SELECT user_id, price, pay_method, date FROM reservations WHERE status = 'paid' {flt}", args
    ):
        try:
            day = datetime.strptime(r["date"], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        ev.append({"uid": r["user_id"], "src": "rent", "m": int(r["price"] or 0),
                   "day": day, "hour": None, "pay": r["pay_method"]})
    return ev


def _item_categories() -> dict[str, str]:
    out: dict[str, str] = {}
    for c in MENU["categories"]:
        label = f"{c.get('emoji', '')} {c['name']}".strip()
        for it in c["items"]:
            out[it["id"]] = label
    return out


def _pct(a: float, b: float) -> int:
    return int(round(100 * a / b)) if b else 0


def _an_date(d) -> str:
    return f"{d:%d.%m.%Y}"


def _guest_label(g: dict, phone: bool = True) -> str:
    who = g.get("first_name") or "Гость"
    if g.get("username"):
        who += f" (@{g['username']})"
    if phone and g.get("phone"):
        who += f" · {g['phone']}"
    return who


def guest_analytics(uid: int) -> dict:
    with _connect() as conn:
        ev = _an_events(conn, uid)
        items = [dict(r) for r in conn.execute(
            "SELECT oi.item_id, MAX(oi.name) AS name, MAX(o.kind) AS okind,"
            " SUM(oi.qty) AS q, SUM(oi.qty * oi.price) AS s"
            " FROM order_items oi JOIN orders o ON o.id = oi.order_id"
            " WHERE o.user_id = ? AND o.status = 'paid'"
            " GROUP BY COALESCE(oi.item_id, oi.name) ORDER BY q DESC, s DESC",
            (uid,),
        )]
        n_cancel = int(conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE user_id = ? AND status = 'cancelled'", (uid,)
        ).fetchone()["n"])
        rsv = [dict(r) for r in conn.execute(
            "SELECT kind, status FROM reservations WHERE user_id = ?", (uid,)
        )]
        bal = _bonus_balance(conn, uid)
        spent_pts = int(conn.execute(
            "SELECT COALESCE(SUM(-delta), 0) AS s FROM bonus_moves WHERE user_id = ?"
            " AND delta < 0 AND kind IN ('spend', 'manual')", (uid,)
        ).fetchone()["s"]) - int(conn.execute(
            "SELECT COALESCE(SUM(delta), 0) AS s FROM bonus_moves WHERE user_id = ?"
            " AND delta > 0 AND kind = 'refund'", (uid,)
        ).fetchone()["s"])
    spent_pts = max(0, spent_pts)
    a: dict = {"items": items, "n_cancel": n_cancel, "balance": bal,
               "pts_spent": spent_pts, "pts_earned": bal + spent_pts}
    by = {k: [e for e in ev if e["src"] == k] for k in ("order", "promo", "offline", "rent")}
    a["m_order"] = sum(e["m"] for e in by["order"])
    a["n_order"] = len(by["order"])
    a["m_promo"] = sum(e["m"] for e in by["promo"])
    a["n_promo"] = len(by["promo"])
    a["m_offline"] = sum(e["m"] for e in by["offline"])
    a["n_offline"] = len(by["offline"])
    a["m_rent"] = sum(e["m"] for e in by["rent"])
    a["n_rent"] = len(by["rent"])
    a["total"] = a["m_order"] + a["m_promo"] + a["m_offline"] + a["m_rent"]
    bot_orders = by["order"] + by["promo"]
    a["avg_check"] = (a["m_order"] + a["m_promo"]) // len(bot_orders) if bot_orders else 0
    days = sorted({e["day"] for e in ev})
    today = _an_today()
    a["visits"] = len(days)
    a["first"] = days[0] if days else None
    a["last"] = days[-1] if days else None
    a["since_last"] = (today - days[-1]).days if days else None
    a["visits_30"] = sum(1 for d in days if (today - d).days < 30)
    a["avg_gap"] = round((days[-1] - days[0]).days / (len(days) - 1)) if len(days) > 1 else None
    a["avg_visit"] = a["total"] // len(days) if days else 0
    months: dict[str, int] = {}
    for e in ev:
        k = f"{e['day']:%Y-%m}"
        months[k] = months.get(k, 0) + e["m"]
    a["months"] = sorted(months.items(), reverse=True)[:6]
    wd: dict[int, int] = {}
    for d in days:
        wd[d.weekday()] = wd.get(d.weekday(), 0) + 1
    a["weekdays"] = sorted(wd.items(), key=lambda x: -x[1])
    tb: dict[str, int] = {}
    for e in bot_orders:
        for h0, h1, lbl in _TIME_BUCKETS:
            if h0 <= e["hour"] < h1:
                tb[lbl] = tb.get(lbl, 0) + 1
    a["time"] = sorted(tb.items(), key=lambda x: -x[1])
    a["n_timed"] = sum(tb.values())
    pays: dict[str, int] = {}
    for e in bot_orders + by["rent"]:
        if e["pay"]:
            pays[e["pay"]] = pays.get(e["pay"], 0) + 1
    a["pays"] = sorted(pays.items(), key=lambda x: -x[1])
    cats_map = _item_categories()
    cats: dict[str, int] = {}
    for it in items:
        if it["okind"] == "promo" or str(it["item_id"] or "").startswith("pr_"):
            c = "🎁 Акции"
        else:
            c = cats_map.get(it["item_id"] or "", "Прочее")
        cats[c] = cats.get(c, 0) + int(it["s"] or 0)
    a["cats"] = sorted(cats.items(), key=lambda x: -x[1])
    a["cats_total"] = sum(cats.values())
    a["rsv_total"] = len(rsv)
    a["rsv_kinds"] = {}
    a["rsv_st"] = {}
    for r in rsv:
        a["rsv_kinds"][r["kind"]] = a["rsv_kinds"].get(r["kind"], 0) + 1
        a["rsv_st"][r["status"]] = a["rsv_st"].get(r["status"], 0) + 1
    seg: list[str] = []
    if not days:
        seg.append("⚪ Ещё не было оплат")
    else:
        sleeping = a["since_last"] > SEG_SLEEP_DAYS
        if a["visits"] == 1 and not sleeping:
            seg.append("🆕 Новый")
        if a["total"] >= SEG_VIP_SPENT or a["visits"] >= SEG_VIP_VISITS:
            seg.append("💎 Ценный гость")
        if a["visits_30"] >= SEG_OFTEN_30:
            seg.append("🔥 Ходит часто")
        elif a["visits"] >= 3 and not sleeping and "💎 Ценный гость" not in seg:
            seg.append("🔁 Постоянный")
        if sleeping:
            seg.append(f"😴 Давно не был ({a['since_last']} дн.)")
    if a["rsv_st"].get("no_show", 0) >= 2:
        seg.append("⚠️ Часто не приходит на брони")
    a["segments"] = seg
    return a


def guest_analytics_text(uid: int) -> str:
    g = db_guest_get(uid) or {}
    a = guest_analytics(uid)
    L = ["📊 <b>Аналитика гостя</b>", f"👤 {esc(_guest_label(g))}",
         f"🏷 {' · '.join(a['segments'])}", ""]
    L.append("<b>💰 Деньги</b>")
    L.append(f"Всего потрачено: <b>{_fmt_money(a['total'])}</b>")
    if a["n_order"]:
        L.append(f"  • заказы в боте: {_fmt_money(a['m_order'])} ({a['n_order']} шт.)")
    if a["n_promo"]:
        L.append(f"  • заказы по акции: {_fmt_money(a['m_promo'])} ({a['n_promo']} шт.)")
    if a["n_offline"]:
        L.append(f"  • чеки вне бота: {_fmt_money(a['m_offline'])} ({a['n_offline']} шт.)")
    if a["n_rent"]:
        L.append(f"  • аренда VIP/PS: {_fmt_money(a['m_rent'])} ({a['n_rent']} шт.)")
    if a["avg_check"] or a["avg_visit"]:
        L.append(f"Средний чек в боте: {_fmt_money(a['avg_check'])} · за визит: {_fmt_money(a['avg_visit'])}")
    if a["months"]:
        L.append("По месяцам: " + " · ".join(
            f"{_MONTHS[int(k[5:7]) - 1]} {k[2:4]} — {_fmt_money(v)}" for k, v in a["months"]))
    L += ["", "<b>📅 Визиты</b>"]
    if a["visits"]:
        L.append(f"Визитов: <b>{a['visits']}</b> · за 30 дней: {a['visits_30']}")
        L.append(f"Первый: {_an_date(a['first'])} · последний: {_an_date(a['last'])}"
                 f" ({'сегодня' if a['since_last'] == 0 else str(a['since_last']) + ' дн. назад'})")
        if a["avg_gap"] is not None:
            L.append(f"Приходит в среднем раз в {max(1, a['avg_gap'])} дн.")
        if a["weekdays"]:
            L.append("Любимые дни: " + ", ".join(WDAYS[d] for d, _ in a["weekdays"][:2]))
        if a["time"]:
            t, n = a["time"][0]
            L.append(f"Обычно заказывает: {t} ({_pct(n, a['n_timed'])}% заказов)")
    else:
        L.append("Оплаченных визитов пока нет")
    L += ["", "<b>❤️ Что заказывает</b>"]
    if a["items"]:
        for i, it in enumerate(a["items"][:5], 1):
            L.append(f"{i}. {esc(it['name'])} — {int(it['q'])} шт · {_fmt_money(it['s'] or 0)}")
        if a["cats"]:
            L.append("Категории: " + " · ".join(
                f"{esc(c)} {_pct(v, a['cats_total'])}%" for c, v in a["cats"][:4]))
    else:
        L.append("Оплаченных заказов в боте пока нет")
    L.append("")
    if a["pays"]:
        L.append("💳 Оплата: " + " · ".join(f"{PAY_METHODS.get(k, k)} {n}" for k, n in a["pays"]))
    L.append(f"🎁 Баллы: получено {_fmt_pts(a['pts_earned'])} · потрачено {_fmt_pts(a['pts_spent'])}"
             f" · баланс <b>{_fmt_pts(a['balance'])}</b>")
    if a["rsv_total"]:
        kinds = " · ".join(f"{RSV_KINDS.get(k, k)} {n}" for k, n in a["rsv_kinds"].items())
        st = a["rsv_st"]
        came = st.get("paid", 0) + st.get("confirmed", 0)
        L.append(f"📌 Брони: {a['rsv_total']} ({kinds})")
        L.append(f"   подтверждено/оплачено {came} · отменено {st.get('cancelled', 0)}"
                 f" · не пришёл {st.get('no_show', 0)}")
    if a["n_cancel"]:
        L.append(f"❌ Отменённых заказов: {a['n_cancel']}")
    L += ["", "ℹ️ Учитываются оплаты через бота, чеки, внесённые через /guest, и аренда."]
    return "\n".join(L)


def loyalty_overview(days: int | None) -> dict:
    """Сводка по программе лояльности за последние days дней (None — всё время)."""
    today = _an_today()
    since = today - timedelta(days=days - 1) if days else None
    with _connect() as conn:
        cards = {int(r["user_id"]): dict(r) for r in conn.execute(
            "SELECT * FROM guests WHERE phone IS NOT NULL")}
        ev = _an_events(conn)
        n_all = n_card = 0
        rev_all = rev_card = 0
        for r in conn.execute(
            "SELECT user_id, total - bonus_used AS m, created_at FROM orders WHERE status = 'paid'"
        ):
            if since and _an_day(r["created_at"]) < since:
                continue
            n_all += 1
            rev_all += int(r["m"] or 0)
            if r["user_id"] and int(r["user_id"]) in cards:
                n_card += 1
                rev_card += int(r["m"] or 0)
        moves = [dict(r) for r in conn.execute(
            "SELECT user_id, delta, kind, created_at FROM bonus_moves")]
        outstanding = sum(int(m["delta"]) for m in moves)
    o = {"days": days, "since": since, "today": today, "cards": len(cards),
         "verified": sum(1 for g in cards.values() if g.get("phone_verified")),
         "new_cards": sum(1 for g in cards.values()
                          if not since or _an_day(g["created_at"]) >= since),
         "rev_all": rev_all, "rev_card": rev_card, "n_all": n_all, "n_card": n_card,
         "outstanding": outstanding}
    pm = [m for m in moves if not since or _an_day(m["created_at"]) >= since]
    o["pts_earned"] = sum(int(m["delta"]) for m in pm
                          if int(m["delta"]) > 0 and m["kind"] in ("accrual", "offline", "manual"))
    o["pts_spent"] = sum(-int(m["delta"]) for m in pm
                         if int(m["delta"]) < 0 and m["kind"] in ("spend", "manual"))
    per: dict[int, dict] = {}
    for e in ev:
        uid = int(e["uid"])
        if uid not in cards:
            continue
        p = per.setdefault(uid, {"all_m": 0, "all_days": set(), "m": 0, "days": set()})
        p["all_m"] += e["m"]
        p["all_days"].add(e["day"])
        if not since or e["day"] >= since:
            p["m"] += e["m"]
            p["days"].add(e["day"])
    active = {u: p for u, p in per.items() if p["days"]}
    o["active"] = len(active)
    o["repeat"] = sum(1 for p in active.values() if len(p["days"]) >= 2)
    o["top"] = [(u, p["m"], len(p["days"])) for u, p in
                sorted(active.items(), key=lambda x: -x[1]["m"])[:5]]
    sleepers = [
        (u, p["all_m"], max(p["all_days"])) for u, p in per.items()
        if len(p["all_days"]) >= 2 and (today - max(p["all_days"])).days > SEG_SLEEP_DAYS
    ]
    sleepers.sort(key=lambda x: -x[1])
    o["sleepers_n"] = len(sleepers)
    o["sleepers"] = sleepers[:5]
    o["names"] = {u: cards[u] for u in cards}
    o["per"] = per
    o["card_days"] = [_an_day(g["created_at"]) for g in cards.values() if g.get("created_at")]
    return o


_LA_PERIODS = {"7": 7, "30": 30, "90": 90, "all": None}


def loyalty_text(o: dict) -> str:
    title = "всё время" if not o["days"] else f"{o['days']} дн."
    span = f" ({o['since']:%d.%m}–{o['today']:%d.%m})" if o["since"] else ""
    avg_card = o["rev_card"] // o["n_card"] if o["n_card"] else 0
    n_other = o["n_all"] - o["n_card"]
    avg_other = (o["rev_all"] - o["rev_card"]) // n_other if n_other else 0
    L = [f"📊 <b>Лояльность · {title}</b>{span}", "",
         f"💳 Карт: <b>{o['cards']}</b> (✅ подтверждено {o['verified']}) · новых: {o['new_cards']}",
         f"👥 Гостей с картой, которые платили: <b>{o['active']}</b> · приходили 2+ раз: "
         f"{o['repeat']} ({_pct(o['repeat'], o['active'])}%)",
         f"💰 Выручка заказов в боте: {_fmt_money(o['rev_all'])}, из них по картам "
         f"{_fmt_money(o['rev_card'])} ({_pct(o['rev_card'], o['rev_all'])}%)",
         f"🧾 Средний чек: с картой {_fmt_money(avg_card) if o['n_card'] else '—'}"
         f" · без карты {_fmt_money(avg_other) if n_other else '—'}",
         f"🎁 Баллы: начислено {_fmt_pts(o['pts_earned'])} · списано {_fmt_pts(o['pts_spent'])}",
         f"   на счетах гостей сейчас: <b>{_fmt_pts(o['outstanding'])}</b> (= ₽ будущих скидок)"]
    if o["top"]:
        L += ["", "🏆 <b>Топ гостей за период</b>"]
        for i, (u, m, n) in enumerate(o["top"], 1):
            L.append(f"{i}. {esc(_guest_label(o['names'][u]))} — {_fmt_money(m)} · визитов {n}")
    L += ["", f"😴 <b>Давно не были</b> (>{SEG_SLEEP_DAYS} дн., ходили 2+ раз): {o['sleepers_n']}"]
    for u, m, last in o["sleepers"]:
        L.append(f"• {esc(_guest_label(o['names'][u]))} — всего {_fmt_money(m)}"
                 f" · последний визит {last:%d.%m}")
    L += ["", "Кнопки ниже открывают карту гостя 👇" if (o["top"] or o["sleepers"]) else ""]
    return "\n".join(L).rstrip()


def kb_loyalty(o: dict, cur: str) -> InlineKeyboardMarkup:
    rows = [[_btn(("• " if k == cur else "") + ("Всё время" if k == "all" else f"{k} дн."),
                  f"la:{k}") for k in _LA_PERIODS]]
    btns = [_btn(f"🏆 {i}. {(o['names'][u].get('first_name') or 'Гость')[:18]}", f"ga:o:{u}")
            for i, (u, _, _) in enumerate(o["top"], 1)]
    btns += [_btn(f"😴 {(o['names'][u].get('first_name') or 'Гость')[:18]}", f"ga:o:{u}")
             for u, _, _ in o["sleepers"]]
    rows += [btns[i : i + 2] for i in range(0, len(btns), 2)]
    rows.append([_btn("✖ Закрыть", "ga:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ======================= ХРАНИЛИЩЕ ДИАЛОГОВ (FSM) ============================
class SQLiteStorage(BaseStorage):
    """FSM в той же SQLite-БД: корзина, стол, шаг брони и т.п. переживают
    перезапуск/деплой. Через FSM_TTL_HOURS бездействия запись сбрасывается
    (вчерашний стол и корзина не «всплывут» на следующий день)."""

    def __init__(self, ttl_hours: int = FSM_TTL_HOURS) -> None:
        self.ttl = float(ttl_hours) * 3600

    @staticmethod
    def _k(key: StorageKey) -> str:
        return (
            f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id or ''}:"
            f"{key.business_connection_id or ''}:{key.destiny}"
        )

    def _row(self, k: str):
        with _connect() as conn:
            row = conn.execute(
                "SELECT state, data, updated_at FROM fsm WHERE k = ?", (k,)
            ).fetchone()
            if row and time.time() - float(row["updated_at"]) > self.ttl:
                conn.execute("DELETE FROM fsm WHERE k = ?", (k,))
                return None
        return row

    def _write(self, k: str, **fields) -> None:
        self._row(k)  # просроченную запись сначала удаляем
        now = time.time()
        with _connect() as conn:
            exists = conn.execute("SELECT 1 FROM fsm WHERE k = ?", (k,)).fetchone()
            if exists:
                sets = ", ".join(f"{f} = ?" for f in fields) + ", updated_at = ?"
                conn.execute(
                    f"UPDATE fsm SET {sets} WHERE k = ?", (*fields.values(), now, k)
                )
            else:
                conn.execute(
                    "INSERT INTO fsm (k, state, data, updated_at) VALUES (?, ?, ?, ?)",
                    (k, fields.get("state"), fields.get("data", "{}"), now),
                )

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        st = state.state if isinstance(state, State) else state
        self._write(self._k(key), state=st)

    async def get_state(self, key: StorageKey) -> str | None:
        row = self._row(self._k(key))
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        js = json.dumps(dict(data), ensure_ascii=False, default=str)
        self._write(self._k(key), data=js)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = self._row(self._k(key))
        if not row:
            return {}
        try:
            d = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    async def close(self) -> None:
        return None

    def purge_expired(self) -> int:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM fsm WHERE updated_at < ?", (time.time() - self.ttl,)
            )
            return cur.rowcount


# --- Статусы бронирования ---
RSV_STATUSES: dict[str, str] = {
    "new": "🟡 Ожидает",
    "confirmed": "✅ Подтверждена",
    "cancelled": "❌ Отменена",
    "no_show": "🚫 Не пришёл",
    "paid": "💳 Оплачен",
}

# --- Способы оплаты заказа (запрашиваем у админа при статусе «Оплачен») ---
PAY_METHODS: dict[str, str] = {
    "cash": "нал",
    "card": "безнал",
    "transfer": "перевод",
}
PAY_BUTTONS: dict[str, str] = {
    "cash": "💵 Наличные",
    "card": "💳 Безнал",
    "transfer": "🔄 Перевод",
}


def _fmt_money(n) -> str:
    """15520 → «15 520 ₽»."""
    return f"{int(n):,}".replace(",", " ") + f" {CURRENCY}"

WELCOME = (
    "💨 Добро пожаловать!\n\n"
    "Это бот кальянной Zig Zag⚡\n\n"
    "Хотите забронировать стол/vip комнату ? Или сделать заказ ?"
)

# --- сценарий лояльности (первый раз / постоянный + телефон) ---
LOYAL_ASK = "💳 Вы первый раз или постоянный гость?"
LOYAL_FIRST = "🌟 Отлично, что зашли!\n\nОставьте номер телефона — заведём карту лояльности 🎁"
LOYAL_REG = "💎 С возвращением!\n\nВведите номер телефона — это номер вашей карты лояльности:"
PHONE_ASK = "📱 Отправьте номер кнопкой ниже или просто напишите его:"
PHONE_SKIP = "⏭ Пропустить"
PHONE_BACK = "🔙 Назад"

ASK_TABLE = "🪑 Введите номер стола цифрами (от 1 до 8), например <b>5</b>:"
ERR_TABLE = "⚠️ Номер стола — число от 1 до 8 (VIP-комната и барная стойка бронируются отдельно). Попробуйте ещё раз:"
ERR_MENU = "⚠️ Эта позиция больше недоступна."
CART_EMPTY = "🛒 Корзина пуста."
ASK_TABLE_FIRST = "Сначала укажите номер стола — отправьте /start"

BTN_MENU = "📋 Меню"
BTN_CART = "🛒 Корзина"
BTN_TABLE = "🪑 Стол: {table} (сменить)"
BTN_BOOK = "📅 Бронирование"
BTN_CARD = "💎 Моя карта"
BTN_PRE = "📝 Предзаказ"


def main_text(table: str) -> str:
    return (
        f"🪑 Стол: <b>{esc(table)}</b>\n\n"
        "Выберите раздел каталога или откройте корзину.\n"
        "Когда всё выберете — оформите заказ, и он сразу уйдёт бармену в канал."
    )


def cart_text(cart: list[dict], table: str | None, title: str = "🛒 <b>Корзина</b>") -> str:
    if not cart:
        return CART_EMPTY
    lines = [title]
    if table:
        lines.append(f"🪑 Стол: <b>{esc(table)}</b>")
    lines.append("")
    total = 0
    for n, i in enumerate(cart, 1):
        sum_i = i["price"] * i["qty"]
        total += sum_i
        lines.append(f"{n}. {esc(i['name'])} × {i['qty']} — {_fmt_money(sum_i)}")
    lines += ["", f"💰 <b>Итого: {_fmt_money(total)}</b>"]
    return "\n".join(lines)


def confirm_text(cart: list[dict], table: str | None, promo: bool = False) -> str:
    total = sum(i["price"] * i["qty"] for i in cart)
    head = "✅ <b>Подтверждение заказа по акции</b>" if promo else "✅ <b>Подтверждение заказа</b>"
    lines = [head, ""]
    lines.append(f"🪑 Стол: <b>{esc(table) if table else 'не указан'}</b>")
    lines.append("")
    for i in cart:
        lines.append(f"• {esc(i['name'])} × {i['qty']} — {_fmt_money(i['price'] * i['qty'])}")
    lines += ["", f"💰 <b>К оплате: {_fmt_money(total)}</b>"]
    if promo:
        lines.append("ℹ️ Акции не суммируются: баллы не начисляются и не списываются")
    lines += ["", "Отправить заказ бармену?"]
    return "\n".join(lines)


def sent_text(order_id: int, table: str, total: int) -> str:
    return (
        f"✅ Заказ <b>#{order_id}</b> отправлен!\n"
        f"🪑 Стол: <b>{esc(table)}</b>\n"
        f"💰 Сумма: <b>{_fmt_money(total)}</b>\n\n"
        "Официант подтвердит заказ — как только он будет готов, вам сообщат.\n"
        "Можете продолжать выбирать: /start"
    )


def table_set_text(table: str) -> str:
    return f"🪑 Стол <b>{esc(table)}</b> установлен.\n\nВыбирайте раздел каталога 👇"


def order_text(order: dict) -> str:
    total = sum(i["price"] * i["qty"] for i in order["items"])
    promo = order.get("kind") == "promo"
    if order.get("kind") == "pre":
        lines = [f"📝 <b>Предзаказ #{order['id']}</b>",
                 f"⏰ К приходу: <b>{_pre_when(order)}</b>"]
        if order.get("rsv_id"):
            lines.append(f"📅 К брони #{order['rsv_id']}")
        lines.append(f"🕒 Оформлен {order['created_at'][8:10]}.{order['created_at'][5:7]}"
                     f" {order['created_at'][11:16]}")
    else:
        head = (f"🔥 <b>Заказ по акции #{order['id']}</b>" if promo
                else f"🧾 <b>Заказ #{order['id']}</b>")
        lines = [
            head,
            f"🪑 Стол: <b>{esc(order['table_no'])}</b>",
            f"🕒 {order['created_at'][11:16]}",
        ]
    who = f"@{esc(order['username'])}" if order.get("username") else ""
    card = guest_card(order.get("user_id"))
    if card:
        bal = db_bonus_balance(order["user_id"])
        lines.append(f"💎 {who} {esc(card['phone'])} · баллов: {_fmt_pts(bal)}".replace("  ", " "))
    elif who:
        lines.append(f"👤 {who} · без карты")
    lines.append("")
    for i in order["items"]:
        lines.append(f"• {esc(i['name'])} × {i['qty']} — {_fmt_money(i['price'] * i['qty'])}")
    lines += ["", f"💰 <b>Итого: {_fmt_money(total)}</b>"]
    used = int(order.get("bonus_used") or 0)
    pending = int(order.get("bonus_pending") or 0)
    if used:
        lines.append(f"🎁 Баллами: −{_fmt_money(used)} · деньгами: <b>{_fmt_money(total - used)}</b>")
    elif pending and order.get("status") != "paid":
        lines.append(f"🎁 Спишем баллами: −{_fmt_money(pending)} · к оплате: <b>{_fmt_money(total - pending)}</b>")
    if int(order.get("bonus_accrued") or 0):
        lines.append(f"💎 Начислено кешбэка: +{_fmt_pts(order['bonus_accrued'])}")
    if promo:
        lines.append("ℹ️ Акция: баллы не начисляются и не списываются")
    lines.append("")
    status = STATUSES.get(order["status"], order["status"])
    if order["status"] == "paid" and order.get("pay_method"):
        status += f" · {PAY_METHODS.get(order['pay_method'], order['pay_method'])}"
    ts = (order.get("updated_at") or "")[11:16]
    lines.append(f"Статус: {status} · {ts}")
    return "\n".join(lines)


def _order_notify_text(o: dict) -> str:
    """Личное сообщение гостю о смене статуса его заказа."""
    pre = o.get("kind") == "pre"
    if pre:
        head = f"📝 Предзаказ <b>#{o['id']}</b> · к <b>{_pre_when(o)}</b>"
    else:
        head = f"🧾 Заказ <b>#{o['id']}</b> · стол <b>{esc(o['table_no'])}</b>"
    s = o["status"]
    if s == "accepted":
        if pre:
            return f"🟡 {head}\nПринят! Подготовим к вашему приходу."
        return f"🟡 {head}\nПринят! Бармен уже занимается вашим заказом."
    if s == "ready":
        if pre:
            return f"🟢 {head}\nГотов и ждёт вас! Подойдите к бармену."
        return f"🟢 {head}\nГотов! Можно забирать у бармена."
    if s == "issued":
        return f"🔵 {head}\nВыдан. Приятного отдыха! 💨"
    if s == "paid":
        m = PAY_METHODS.get(o.get("pay_method") or "", "")
        tail = f" ({m})" if m else ""
        return f"💵 {head}\nОплата получена{tail}. Спасибо!"
    if s == "cancelled":
        return f"❌ {head}\nК сожалению, заказ отменён. Вопросы — администратору."
    return f"📋 {head}\nСтатус: {STATUSES.get(s, s)}"


# ============================== КЛАВИАТУРЫ ==================================


def kb_main(table: str | None) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_MENU), KeyboardButton(text=BTN_CART)]]
    rows.append([KeyboardButton(text=BTN_BOOK), KeyboardButton(text=BTN_CARD)])
    last = [KeyboardButton(text=BTN_PRE)]
    if table:
        last.append(KeyboardButton(text=BTN_TABLE.format(table=table)))
    rows.append(last)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# --- фотоакции и правила (кнопки в меню показывают картинку) ---
# Картинка берётся из файла рядом с bot.py; если файла нет — из встроенной копии.
PROMO_PHOTO = Path(__file__).with_name("promo.jpg")   # «🔥 Выгода»
RULES_PHOTO = Path(__file__).with_name("rules.jpg")   # «📜 Правила»
PROMO_PHOTO_B64 = (
    "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"
    "smLo8yzgPmgmFZGQQOi7mLYkHxrI2yESxhsWjVtia0wL7PzzqJmDWdUe0GxhkGrUlUa1i9WgjPG2"
    "dS+hlhGsDr6aVY/mvvVhDT5YyFgzJQ+xLZMhY4hr5GyoQ13RDAKQyGKcp832G/Hl/EPR19sPG7aF"
    "O0FnaCztBZ2gs7QWdoLO2FnQFnaCztB52Q87IedkPO2FnaCztB52gs7YWdsLO2FnbCztBZ2gs7YW"
    "doLO0FnbCzthZ2gs7YWdoLO0FnbCzthZ2w87YWdAedoLO0FnaCxsIWvbDx0QXVHEHo5geiQh5tw8"
    "7QSYkIedkLO2HnQHisDxYw9PDp5p8eX84lScqSsVi5ANKVIUBKEmRCzTtljdDJz1xOWvmvimQwPI"
    "dMPIOvnr4QjSkNmidBI3k/5TJPhntjn4jeWua5rr4p75ojc16uSfPJfjm35cmJ/UpT3fUShMLklQ"
    "MQ5jIayujrhrFB9oeTFX94W5hHheDVtPrmRRvOshYdp2hFsWBQSVcFZt43BRONFp2IQExshcG0mt"
    "WsgBr7KCJjIuwNU7EcyeDa2aM24oLq6B9n6Y2Ktl2YpzavRtkKPFWgwRyiNFjfTSBhDkQ10cwC1E"
    "fQ+EJyxdD5bMcOMWsHYUslYo9TEBDKo4HfCuhx40Bgi9Mt4mQTQ9ker2Q5M8e2s2t7AoThGR2pQk"
    "GOcF6o8eCsGiCGnMKBZXhc0ZriaJj11VOTPnk745s/Ngs6iTbiTcyWEvSy5nbg9hKPk1lPKr7mdy"
    "OsZ3AzkvITdSbX1idZVt5VRlhMwqKxdGNJaESYywmjdAXIPNE9YJYz5ojR7GWCEouQyRSXuF386L"
    "LayvjJMkKc+xnkmYS+MZt1OmQWRI8Skv2ohrw09VbKF6xMubt6CqbL2ZbaSZuK5XYiZEU9gvrMzX"
    "MsJtjMS+aOCwfAPOZIbikvUX1SZrn28j2F3bXykXPcI9UfNIVeSbweylgjW1IUthsjIOTWcn/HNn"
    "Nfjmz82NcrHbqXJDZs3s2b2XN5Nm8mzeTZvZs3k2bybN5Nm8mzeTZvZc3k2bubN3Nm8mzeTZvJc3"
    "kubybN3NjS5lXdy5vJs3c2bubN3Nm7mzezZvJs3k2bubN3Nm7mzdy5vpcYbNo4ubRhc2KbNimTYh"
    "0yZvZ83s2b2bN5NiGTZvZs3k2b2bN5Nm9mzeTYhcq819/Bqf0584/wBvCP55yfPj0/0+afYT2V3z"
    "mma8tfHTlpnSmM9uTnZ8Zr9vTTkntzVc15s/LmmSeEXhJ8+LV0xzfuDwPKnJGeLPBXTTijiylSLw"
    "ueqECTByi105sWDjyFTFCyBEfS1hovC9giIzXIOHD5mGU5gDeevsi+2L9tvJOeuO+U5JifPViY/5"
    "5xfHN/5tPJrtMVv2deVJ+rWFIYZc3R8eOmWloUnkV0M3rVLwvMgol7XMHJ4fHZXR8QtT1m/qizSp"
    "qWyhZw4PH1m2M50tDZSRlWo6BWC/f+c6NExnJy434d+bFxPBfjmzEzXk/wDN5td04rftUf6vZ25Y"
    "V5dhxkwsj9do9iU2RsS0VNSO1qKYmA8aWx3t5xFr61xSj97w00tD6osdlgfUlhS0dVI2ezI3py+/"
    "29eWiriMz4TTqb0LjU05Oxvx/OLic0z+ObOS4mO+ea+LXaYqapmvnBO8eUieQqYewIFihnkgenEt"
    "n0yzyESDmzQRaJkUjoZCCZCpvqOwTJ7s4tmmQX1gK0u2KOzXX7mnJM1xVxi6pzd8J8ctNV7a50Ln"
    "RnRnR4N+OSY755Ji84Ru9AwbrEhBHmXRY3tBHlUcJj0nrk09Nie7artEBgZMtZC45tbFI+EJFhkB"
    "6XND6rHafh0F1B9PiidELKO+Ud81aLDuMjg70EAY0uIE7pdErBVrXbqQKPtCzNHnmFhq4p40JCjZ"
    "HEbstm8BqSk2UbVdTta6xZaTK6YBCrf0+KVsYUbYSxXCvzq5/GI9cSTOrw6c6cav2E/LyTHfOJ41"
    "5m0HdYuIroyoWETuR5AssQizzskFjOYPG4nRw1m4QKbb7sRzJeIYYhByu7GcK6eGBrLl/qsB+3Gf"
    "ZLPWkIKYTDYjSzLYd8UYp4srbBjwmFRJiEQTSSRQOD9QiiPIIe2ANsLp/UWnJXzxgNBmRlgOYxJd"
    "3GCJvWEhVcjYDWezhpIZ7WFRwHO7BsB08b0+U5Ivgnun8+Gua5riL4ovt1ZrnXi/PgqeKYuJn5+a"
    "ZFK6GTwXFT7Wv2E9s+VildC5fnkn2NcX3+wvgnP2xftJ4L/WnLXOrOrOrNeS/Gn7D4zuYi6r/K+/"
    "L+U+FxMVvtyTRUVPD5Xmiarpn880+MHZFJK4EeURsT5V7L1iSoOVo4BJbWwSulIDIFWauKgifBJG"
    "54k0WLE9I315cUQwRBaKNMmMFmekQ8ssjoX9EsT4JNtMkkVcXPjRZnzT15QzJYnwugryiWbaZF7T"
    "+06uLZD/AB9pE1z48E9lX55a51cmJjs05JmuaZp4a5rrie3knxggrzSLEWcUWkc1pJcK2I0c3VxI"
    "GJ1ATuRbIKKevLJCsImGzQgqcXCyx7wjBntmHn7UpdcP/Zj9pLUSeESyqpGCDFxyMIfLB3xwXbOZ"
    "Om3Y/qoyBmkGzOgeKskbCnvHloxhtuc3F+fss/N84rEzT7KPxV5rz189fFM05u+fJcYuIxOTnIma"
    "9SOxOS43+vNPNnJeen2EfmvLTEZ1K6F7U+yiK7O2/TnrmvPXNfFvIIdCixqmSUgUQadhoqhFgDw2"
    "4wxTLEpY/TgCKtJSR2+qWVr0Gj0LUdbh25EpqBxFTSAQyjenCRzzxOik5LnxiL1I5OWntz0/o5p5"
    "h2M75UhH+pLH1LskkekMaDBMcCX6vNQfrEZds8+4iiZZVvD6RV7V6VY6VRYpuyjotw1GS95BYg2v"
    "eNMMIU5IYSCSE+635wEhBTBreSMlljBtLMpppy26wI6cCQ2wOfYFT2i71bWEZjLqSWKtKaCeNMkJ"
    "gBET4ZCR4BnnAvIlldPK9MTkuNz5xyaKxcVOcfunwv2RpK+AHshsLhkCrInSB2UPrETD4ZAKx1IX"
    "CGeddykJrlbfPHEgjZI6HZiOHmER0xz+pVGWZk7ZkkfHFAISyCKYl833m/OM6Ef3A8WQLO4FncDz"
    "uBZ3Qs7oWd4PO6HiyBZ3Q8ZIHr1h5JIHjZQ87geK8JF7oedwPO6FiTB53A3Yrw2r3Q87gedwPGSh"
    "ornBrncDzuhZ3A87oendCzuhZ3Qs7gedwPO4HndCzrCzUPOoPOsHOsLO6HncDxZgcR4edYedYeLI"
    "HncCzuB53Q87oedwPT7qfOCRJOVFXscTCBOW11aU0gmvIEb6Ob2x6wsmNRJmNQEhYmizPnGriS2I"
    "EQpMtWWPiBkLJJDKxjQCYinjRzxOyYdw+DCylyTVJcMUdObI1gZDpyQJx2kCzBv2BKTRCTENhqjJ"
    "2QCTESFVhI+SxPgkmgfCmwI70EDyJRx29swZYk8Nc1xVzqzXNeSJyVcTF5MT2VGpiYjdc7eaYqcu"
    "hc6VzRfsLlc5GnusEsJZ27WodtiDZHxj1pwm5LniSzgA2zRSbRZ60M8OY6F0Zdaw1HmNDUKUwiA1"
    "GzjD1yTRy3EB8PZildDLY3EhkVNI1kwL2tq7AJ5cO/GeWZI8cM08WUlhLZrgc+FjBoh2xzKxsvcZ"
    "6eYsT5yTB2DNIjW8GdBHOE8T0qzO3td5Ly9s0TNOSuxqe/P4x68mfCr7InJrffm53TnVi+en2lzR"
    "cRFXG/0pIi6M+UxyYxjnIrHNwkh5C4nk12qKmuKmi5/Hh6MekcMLyHk1hYjBq0oxqxPbLPVmCxCj"
    "PMI9BK1e1Y3RxPldHC6VXiyxMx7HRuQSbpYx0qzRPiUcCclHV74onZrie/iq68tcVFTxAgaUbNW7"
    "KEWohKwQYfbeljSHtqCOrYAOn9IlmBsgthJFVPJrZ6xoriqpiA15ThCriFtMO4ZLjBJ/USDWemA1"
    "6bIUod4pCFTCUsxMpVCSHWhSOqYhXkiQODxeSJripifOOTXy1VFqElZPSu/GwMgZX3T3R5F/WVQS"
    "PlseHv1n0tIzbYppthVBiQ0SLpk6NkGEFk7bayVJv7bD5ZIR4YzJVBEc6eOLpF5t9uS8nLr4qzmm"
    "CE7UttiroW3MKGTmtUKO36CfUB44H387ppDVeIaZu13q+nxXCxPdYjtEBLaFMyxXarYMjCEK2rBz"
    "kaJNdlSzmGb1ZyusBhv/AB1kYGhHrCvkKsGyDouvJflG9LVxvsjXcnN8NPZie89wOQkJ6iTPtI2R"
    "Q2jOw61IU59uxrK6wlrSSjyDOQdhMFgssUS7/Rjy55EkJc/N77xEqzJSOtkJckMb3ukXGp4OXxR/"
    "J7ecTkY/cxZuYscRFm6izdRZuos3UWbqLN1Fm6izdR5uos3UWbuLN1Fm6izdRYhUWbmJU3USY4qL"
    "NzEubmLN1FiFR6IREuKTHpuYscTEmIVFm7jzdRY4iLN1Fm6ixSosQmPTcxYpMWm6izdRZuos3UWb"
    "qLN1Fm6izdRZuosQqLNzFikxZuos3cWbmLN1FikxaNb4OXzY7kuJg0KTyy1zNu1jpFkHlY1IJXYj"
    "VVZI3xq5rmO7b9UarsbDK9scb5FfDJHip0qrVTO0/VGq9Va5G6OYujm52JXPa13W+CWNHMXGQySY"
    "kb0VqLiwydtMk+cTEXpX+HJza7kvx9tfFG81dmuv2E+VXHchh5CpjYpARKVytngJmJq7BbFImpH6"
    "gfK5QbNgpZqPUq8GWEYYFs0GaPElfK+cYsKAso3Zdh+nrteHC2eo6Owf397Ht1UeMhgssa+uxFTF"
    "Uso0JU717YLuj1Zdr6QKyRhzk6ZHfOJjkxrtOTm8kXRc/j+UzTNPto3XEbpy60xXa+SJri+y8m+6"
    "qmO5RvdGriZno1ytxHORCrGSZyOVFkmklxznPXuvRUcrcbPK1iyyPb1ux5E0iK9XYk0qPdPK5yvc"
    "qOe569blyMmXHTS417kRSZkTrc5O6/XqXRhM3Q/PnFxjcf78mu5OTkzkvLX7DdM0TNG5/SmK/Fcv"
    "mvJq47Tm1OnFz5XAR92SKEpBAwo8sZI7hJxwYLSEdw1kQyBoleRTdwiGJlnZ2scEsdJEye1FObKa"
    "2rQoietRIEpmJJIx0b/CNfdU9sdy1x2R47EXTOnVfjH82Ox2J/bfArFrquGGVjezZizdiqiSqHlP"
    "EePbSUKI+3bakSnW48YtlWcPK4Jr+h0mjQxYk7TmPklZEksqAIM17BJIBp4mRwlSGp4/x4aYrvdz"
    "teSZXTtGPGvJe+OfCwS1JYWe22QFi7FT7U9bEue26TWnCAIy1ZOPUlMDsRZmxGgyQSxbkYIRxNdK"
    "YRMpM/gi5/KJ7rzX4T2Vcd8xr7Zoi4qc2rqkbWvkBY2snrCYWqnZqxp2wWsfqY8RorIKiSimggsj"
    "OIJVVVVy1t+rQR4O+5jYhYxyRmwusNux6tkn7rSWv6Bxwp44Y5zHyp4/x4I5Fx3zzb09SbXNBs/D"
    "Z+FzQXPwufhc0FzQXPwufhMR47W/hc/C5+Fxdpn4TPwmfhc/C5+FzQXPwufhc/C50i5oLjdsqK0b"
    "GtGTPwuai5+EVPwufhcTa6oouioMrfwuq7bF2ufhdHbXPwuaC5+FTPwufhc/C5+Fz8Ln4TPwufhc"
    "Ta4u18v459Pt5J4L+wTk7G5pzTkvNvNcT5cujU+c/li45uvNfLX7zfjwXknNcX7C/YTkia4nsuT1"
    "08cBQUwWa8n80TTF+cTl/DvzYnziLrj05LjfnF8E+7/HJOXzz18oIHkyvq6utz0YGxYDGEySKspZ"
    "gj21SQsrquGtArqSxnjiR5t2DHXWGC10M1JU1w1pAFXTGG28Ao5dJVxnPtwPTjqIOOxO3hIEoNC4"
    "8TKQKM8+WChjeclT2KuofZZ2qBi2tLtoaYJlhYvi4fjfbVgcALK+L0L/ALMTV97XxVp4ldFNS10S"
    "EH2QrBbA2spq55LaTstTP4xvyrfFPtNXTlpyXmmJz0xycuEWpu5JHSyVsr4T+J42suAP9XXLn+im"
    "4R/WIP1S9KrYrKICuuIK/wD1YWWUcix6xa5vzYsWupjmraUXCf6wBWtLPAs3WRKZwsv/ADJUVJuD"
    "KWHZnLt+F2r78Pf3A+F2f8xKvVNY/wCugSwR8NRlUkrrOt9MP4u/V6//AFam/VrtdLy+ECIIOCBg"
    "hzYgq2QAFjExHYrdc6fuKnNMXPnPjkmJ8815Vdg6sMnqBLJ4oQVG4wp5hQMjE4Zqqx9oRcVS2ElH"
    "UemHlVz6y1uql9hYhMg4eiBkZ9NUkUAcQN9LFZspx47onigx09ZxBMUVRxsB4gvrSN68NyMZy4Ze"
    "2O4IoZpZ3uHpqmrsRpgl4aVXFGC1oAEMVMI1MspGLQARoZw5Hw6rFu7CI0+9qlszhazs0wFE8U26"
    "cj7q5r/Up5qCSKJU0xeS8kzqz58Wpqv2V98amO8U5L51Vl6ZLys7L1JfJuaey8kXHJy/682Lzdy0"
    "xc/hOTeTXe645PPo91TmxMVun2HLjcX48G8teS+TeGbB7Ppaxw0KYCcGkLsIfpawwiB4s4gcp05Q"
    "sgU4dGWdCvCtjk9QSMUQO8WYWtnsJZI3RSDxOnmLDlCmcmmS1xEIQFMVZRfS1hjwJozSxZAiAhJD"
    "5UGepJg0gRODATGRo3rcYDLXzRgTPBAqyLJU4XsMNBmr5G/mxUxyeWmuKnNnxnQmKzO2uKip4fKo"
    "mPXxT7XE7lSIV7tzxZ+s/l4TonqtxefrFd/xVNcJ6lW9Sply5fQ6hVW1t43zcQ13ZrCLBNDqn2sb"
    "+Bli2qr/AFAziImMqipYpJ+HfQ7TKxrmW19UHE2vD1SaLaRf7BeVG5tCaAkcfh3/AAof8nilP+XG"
    "TThPhRquiSktcnikgkj+cR+Ljk08Fxi47miePzyVqarjW4748k+OWnsqePFH/wAoX/J4qYr7q0jU"
    "Lh+i/WLAVxnENsXUo6qIqp2kQuGnuf0Ko/VbN0NHNw1M6a5PTUyrT/kjrB1dxNbTDVohn+p07ns4"
    "d3Vllb1eq8Qmkw2/DZpM1vH+v8SMct3w3HKONw77hwwS7nieN7raJjmcKcK6pEhVlk/cVzPhfjGu"
    "5KmnNeSLri4nz4Lmua45NU090yTzT2Xk38rvjwKPnNRrlY71cvelmznSwTvGmjsiYjHvWR0UroZC"
    "ipTZpjpyIIpnwSFmTHSiFShTLK6VzJHRPmnlLIX4ebO8QO2MrmfU1pktgRKWSTIXMKVKFN35EI+p"
    "rPCLUw9A7Emvz6ns8+prPC704yAGyJrl+p7TDbAixcnsj/jk12L8cl8UdpiOTyTG5/Guv2GfOKvv"
    "+bJG/sUxF5P/AC/KOxE9pOXTzjxfb7D/AJ7b+bV9uS/Y61xHZqnhJ8eAcaCjg1zHmbQQodKWGLI6"
    "h2pADha9laC40EWGVtqEgM37FMTJPioHQgy06rAaQUAWH0VrySK2Ngs1HIh7AB0KLFFQVamGSAGB"
    "pRlfTNIdEMGyviEAUJKqMzIgwDJBq2JwT6qGImcUeYSMGXemwtHMXE5rifbRy8nrqvOJ7WSz3Zss"
    "wVk0gx1jBGO60gISM/tQBFRzRpbC7+Y0WFLE5DpHfeXwZ8yfA1i8MWK4nWMwoXb+tP3RRAz2S2zp"
    "TfUREGSwE2XqsDHjE7Mr1l7rBxfWCLPDFRutljVLQeF0FhFs33HXNOfHtjLx5RBMqlEoxMe3TmqY"
    "mKmafad+XxfTNilJGkFnztvxfbG+2fw/qz3zpXNMRquxGKuKxyYmaLp4t8o8X46V5KipipiMVVcx"
    "Wp0uTGtVcVF1d74EK40llXE+SaF0U7kVM6V0GgSRhLWsf0OxsDtEaukMbZFnRkb+0uvRois1T4xP"
    "fF9+a+CZpyRfbF8LJ7G2e1fLbudAyosDyRnWqC+shsEWrRJEs+skisNghsHLKwiwBYFvAlmGDZNI"
    "PxRXEyFGVgUO4mcTtfFvz5IuqVyRLXNjT1QeUskcpgbg5E0sxHkEECymzlNnnAqVjdPe/h4a2i/V"
    "wf1ON7xyu7uxZp3MKhFjkqXujLMeVadBZ5HpxE6jmiDMNrSJYphkhf6nExEqpZljM6RnUxSyR5qu"
    "rl8m8+nNM08PUjdGzyNcyR3be971fI+V/cf0d+XJipice98ipNKitkexzC54G9+XuxESwPcaS+RZ"
    "XrH4p5tdorXO6HSSdMpZBCOkc9m4l7kxMxCyGESxyTyTruZutJHtyN7ono9WucRM+PrerGlkuj7j"
    "1Y6V7kkLnlY+V8jWmEsj7r+2yZ/T6kZhBRE6bwntJK5zO/NG7NfZvJeaey8tM0z4xc0xU5p8u8G+"
    "C/bXzjXHflz/AK805J4aa41NMXFXTPnkvNrtUXmvun2V+PFiM64YgjkGGeXLEM+aKOomnjGrpisW"
    "rmScqvkEZ4r5xOa2S0ggjQ0ccLI4RhQPSopyY4grDKqGGXGQiHj8hZRYWWkEUWSCvig9Om7xNbKM"
    "x9IQiDVUpLJI3QyImuL7Z84me2N5Of4rgIamzNGaXZxRgGztHHEDPFjiaSgQUdoJGLLyTEzTmzNP"
    "bxYzqeVHC0UMp4REZw7whZRIZnTQGjCxBDknERekckxftWEscsYcqikOdDZBJZQCkDpBVrSlDiIX"
    "avlZyqIRVyy63TQ2KMFlIDIsZSo2V5cIxRAyititZWSWCu8OrOpc115J4jWUY4c1uLEQM0UApHRn"
    "g2M8XQVaDMhJJkMlXzb84kmdWdWa8m9Kr0D50DYjBs6R86Rs6Rs6Rs6Bs6RsawfOgZcWEbOyPixj"
    "50DZ0DZ0jZ0DZ0DZ0jZ0jZ0DZ0DZ0DZ0DZ0jY5sGnJjYdOkbOkbOgbGoNn4dM/D5oNnSNnSNnSNn"
    "QNiRjZ0DYrBs6Bs6BsRg2dsbOkbOkbFjGzpGzpGxEGzoGzpGyRIsVPtJ7pzVVbjNX50++fHijPJz"
    "fbwa1zlVFavafoxjn472VzHNRrVdjmOb4KipyTP4xU8UTXn/ABmmJ7Zrry0TPnJY3Rqsb0axrnrp"
    "naeqZ/GngmLyZ+XG/OnPpV6tAlgayP8AvMp19WCFilhkqIoDH7GVZquFwbE08VfpnWuN1x7fAQsm"
    "BLt/9Es0pkj5ZK2qZEwueuMnspuH+5qu7YByqwNytpCTIgYO6CkA7Ar6qF4jhAwYxBR5ltgGgEc2"
    "r4OTExcRedLFoHCLKYSDYTm2csr62qsl7wk7LFYLrRStM09ubfnT36cbzb8cmuVrhLYkaWSR8jg7"
    "KYaZp8e0fcq8tLKGCVLKFGeCvxExG6cnLpmuLyrLCOuUggeTI7IWCWKyb2JLaTdOsoY2AWcgEc0z"
    "538oipx0KtZyhI5Xwubay6eqQsRtjFJG2wjWA8tDXObp9jXTxCN2qOtOhjrWKNw57EGMPUqQm3lK"
    "ZyXHN5t/N8LnVp5RKrHKXLm8kzdyY82XN7Lm9lxhUmm8kzeSZvJMedLm9lxpsiopcmb2TFNlzeSZ"
    "vJc3kmb2XN7Jm9kzeSZvZc3sub2XHFSPbiYwh8SbyXN7LkZcubyTN5JimS5vZMU2TFLkzeSZvJMa"
    "bLm8lzeSYpsmKZKqb2TN2/N7JrvZc3kub2XN5Lm8kzeS4w2TJJnS+C/OJ8vT38W5pry9KcxMTF9+"
    "WnuvticlXTmi6cl9ld4C1u6aQI8ct9NM1Bq55EMlbPGVNUvjiBBcc6WqkZDyDClNcWKgyJiJomma"
    "cv5198VNfFeTk0zqxcb8CVqlxS1D4oX0srEirXzxFiSBSpTuSMkeQSXm9PbkvxyYiaLzauqDK1pJ"
    "UcE1uLIwKxFgHbIE+USB8W0mAfMaVFOQbAydsWWDFjOd4Mdi+NdO+umZBDX8RVo80V6aneqBf7eU"
    "0b4ZeHx+6x7xauHlWdr0c2CB1cG4eavWHqfNM0yrLnJFIGdMIJeRpFZriLyX3xUxPBMX25IumRK9"
    "+MIjHtq2GWK5OTvVNq1WCHgQPgsSmFS51ZriOxzdFxvu1fnI198enuiY32xjetyTGpE4VyZHDLFk"
    "bzIY27ls8m5nfO8slrCDo2PGe5do7No7No7No7Nm7EEdm0dm0dm0dm0dkE5wzHjySPfKbLCPug1f"
    "FNJJPIaUyOGaJHCqxvIXvROJ3Jbto7EYQmSymkNilNhihlMHZJHLO7aOzZuxBXZtXZtHYoTsQV2b"
    "N2bN2bN2KG5ybN2bN2QjyxPUSRzpHmSxD7kTJxpZZHQzS5LD2vBUz5TGLjm+D+bVxV8P5xOSr9xf"
    "2Gumfzy10zXlp4u+cRdMTl1Z+ZETF5ouLyXkjtcVE5Rr7clxPbD214RJdejjJKcqONw72QkAzBrB"
    "Xyksjrp3SSVxEc6CSvRtMU5g9bMSyOtJknKCmDwUKYxXVRDJSaqcNgAcUkJqw6yDggu9IYPPILAU"
    "I8WvHKgA/wCXihriTJGduTwrRoJIRogDUgqiSIFrCWljATmOijdNJPVzjw+jFdG2k2zawjvTVZEU"
    "hFZOPCymLezG0xbmwU5BEckG3mbCAoVYLHNFGNCeYwYE/EHECFsBWiymRVwU9kGgJacl8EXF8mey"
    "47Exy5cyslshpWSXYscQBMRQ0FXaEbllePpjnCEFSkwJMSVCeKE4Mc4GDRzTRZJ7OZWjhOjIrjXx"
    "j1txKyWepN6ArKb8ATDFaTPPgOm1ZVhTQQFnClxT35NwyOX5XwAlYwGolZE9B0JooSIBzILCCEmC"
    "PuyzrHBWCqKOcHYQiVrYhN4X2pQJ3MHrJiIyCXzdZUrIZ7McyOWaLRzxNQUr2yq1T4x5R4oql72R"
    "2gtlPDLPY2gzZSCJCpU5ri80XXF8E5J7o5NcX25jEPGlXE5r8rzFElMlJEhHi8U8tPsPIkfB9iCr"
    "V8RMbI5uaL4LzTwamOxcTm7w05MXk/5iYsksf95/fnEo4YIiCJyI5hTBxXsVvbvZ5ZTKSBquLhdG"
    "yWWUJWUr+mBND60iWMU5r9nViuklwckkwtpBMtyrlArXPgjVR44aYwcRxcjUiIJc4uoxU8azrhF7"
    "TH2ws5BR8SsDrINvLaQDjMqkJnDb0PiOuul40MojY53RPeec+B+1hGlcopspZpiyGETgSjyywUkT"
    "GnsGnnLOewYyJ8UTrPbjx0xU04tjYxRwH+C4vh/Hl/EnJ1sa5EtZYREkKKIJNMmSSZ8uMuSYovWS"
    "nweqm9PKKV8DyTiDMbZmMiHMnEVTCe+yzmeXPbFSqOaQJksshEiSvbG+eSV7TCGkEFTluzTFTwHM"
    "nFwWzkYdLZlSxwSnhw7ufuwkSQZPfnSzT3JMpBBc5XOCyLFjhLmikKJmMx9kZLFDZFjxw2jx6+Yq"
    "YmSWzLlixTJ3ObM9sTbAyKHE5JyX45afZauL7ony2jR9oXEkBJbxwnQ0r+7PSK6SdlaiTD14rfTI"
    "G2xIgi16U0M0Qo1dMH6XuAXUjutoQJeWLtQfsJzVOYobZwS6hA6uhjjV5TfUh/p+NJIwIB4BhAiS"
    "9qDOBCGCkLa+BDjABgJiq8GI+CimWwhFGiEDEYJY+SLycmNxqcnYvJEx32U9uS/0uMvHFGEy98hL"
    "EUhW3HW9T44Z5iwntMM3SzWsb5Yzu2H6wOhA5SQhoera/wBXe1fUhoEmljfH9nq9+SpyrbT02Fx/"
    "XXgGbOSSwhiGksxSHDnRbZh8MM4xaQCkHseT6tHItgcwpk1iktnBavisBj4ds+wjmmcmi8k5a4uI"
    "uL8Jnxi8lxOSp4a+C8k98d8ck/aa5ryTmqffbj01TkmKvgnw1OTs/jG/Kcnt9kxU824jetVjcmdK"
    "50rp0O10XOh2jWK5Ol2dLtOhc7Ts6Ha9t+dt+KxW/a115Jz0xU+6mfGPTE5aZp4ri47k35T2xZM6"
    "lxfH+OSfOd3pZ3+pyzoibjRWSKxFn1aj06Vn/q3C5uFVO8mLLpjpc7ntJL3Pt68k5afdTEXF5aaL"
    "z1zqxPfkq++LyR+iKqrzXlrmufwvxzTFTXE/cpyb8/eTE5KuL4JiYvxmv29NMX45pifvNc6lxHeG"
    "n2mrjl/pz+ObcTHL9lfBffF+Pup5aftF+188kxeaJpironmnkj8X3TxX96n2F+0vJie2PX7K+TXa"
    "YvgnJfn7y/tlZi/ZRM6861+0vjvZ83s+b2fN7Pm9nzez5vp835Gb6fN7Pm9nzez5vZ83s+b2fN7P"
    "m9nzez5vp830+b2fN9Pm+nzez5vZ83s+b2fN7Pm9nzez5vZ83s+b2fN7Pm9nzez5vZ83s+b2fN8R"
    "m+IzfEZvp830+b4jN+Rm/Izez5vZ83s+b2fN7Pm9nzez5viM3s+b2fN7Pm9nzez5vZ83s+b2fN7P"
    "m9nzez5vp83s+b2fN7Pm9nzfT/8A8jyIrl9HNz0Y3CA5xf2Y9MUQ2ekKhb+02BPZ/d1f6hZmERne"
    "oFYW90tF+xphmSzG2Mpkgh8wj7mCP9kIFMa7oBqcrz5T0/d1f6hb/qOT/wCv/saPR6PY6J7Wq5bf"
    "+wH+xrLGMZhFN1spGq1P3cEyjzPswJnb6sw+xjIg/YxSuhk34JyIbXg5PO8mX9kOTKK9b7qh/wDT"
    "3//EABQRAQAAAAAAAAAAAAAAAAAAALD/2gAIAQMBAT8BNA//xAAWEQADAAAAAAAAAAAAAAAAAAAx"
    "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"
    "EXZ6tkNG0NuCJCA9CXLVYcsqquGpkq9KrBOvy4GZJQuuAMQRCBvDFdg7PhGKzLiBpp+b6tkKwrSY"
    "SldTrrWvvh92TZBZh2ilcVLvbCMOWkRVJyiZKq6wBsywiYLUV2QcsTaKwaqpDtqtYFyZZEzHKvVs"
    "gZVWBwBzQdkYKMJh3IaptXr2w4TrSErreGfWMC8gIjghhov5uyDdflhMz43XCCOSJkn/AIy5192f"
    "dHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o519"
    "2fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5"
    "192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7"
    "o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvu"
    "z7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHO"
    "vuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fdHOvuz7o5192fd"
    "HOvuz7v/AF2Sqa6fkYGmBU3DWginTCszjRMup0F9NEFAl93GQyTijMin8f69Qz26wI7umaf3WVXz"
    "Pzy/r/Y3nKXuEpLRKeTlum2y0cybljRv8m0Kan/CFlmxe/4ld87xIi2nqREiZm5vclvcl9p0QYUB"
    "txF6UpTZEtKKtqOnwl6tVh6T3K3Nkhl2CVurrVxEqdNYmppEl9z5ZsUJ4tAD1JDc5LToTm5yuIDj"
    "zYcJv1jDmLMom5wMY+/LMlH1beqJfdF2fbl5ZwiQlcDi509qxKG3NMvyc2VGpnij/wB2yH3NzN1G"
    "J15gFccaQVHg9W2NzpzFv34h8C3i2rEnIb7pvmWx78Li65Ur1ROzOJgsSwrQra3qnRBzIbsiOC2J"
    "vpvZfF19sbpvyU5vluSw7VRqmJd+EUJKLDG6uNXFewsOzTXOvsjco99Npv8Abvq4lot6ar7YmJnc"
    "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"
    "bHvP5o5mx7z+aOZse8/mjmbHvP5o5mz7z+aOZs+8/mjmbHvP5o5mx7z+aOZse8/mjmbHvP5o5mx7"
    "z+aOZse8/mhf7oz7z+aOZse8/mhE3mx7z+aOZse8/mjmbPvP5oylGfefzRzNn3n80czY95/NHMmP"
    "efzRzNj3n80czZ95/NHM2fefzRzNn3n80czY95/NHM2PefzRzJj3n80cyY95/NHMmPefzRzNj3n8"
    "0cyY95/NHM2PefzRzNj3n80czY95/NHM2PefzRzNj3n80czY95/NHMmPefzRzNj3n80cyY95/NHM"
    "2fefzfWU+ggK6DSekekG/KTIzAN8pwbVGKAKkvVFTaMU2qMcFs1yrxYoiZxRwCFdipFDRRXri2wq"
    "0rpC2oq0hSBsiFOlEijYEa9SQl7ZCq6VGKFksJUVSukKlhVTPSKCiquxIRVFURdFihoo+uEqi56Q"
    "oi2al0pbFlq3bIqbZim1RjhJSPFtkf6KVgkUSqOuWkVRFpti+wrPSpl9OqeTZ+QA1LjcZaQcpLsu"
    "qK5zD1i0LqTqiZIVoqSrmcbpI+6blEbpcVfOjc7eGLh73DkvS64nLOeb3HkqVu8+nXDYvMzVcXgO"
    "TGqbUhx2ZUQWUXxg/wCIPR3RKFaiE5JaJ+iUT8lL0cUZYied2lsTqiQHEnHbxEkwuTFNkbsvNVaE"
    "eABJlmpdEbim8auHjnmS/nDG6F0q9Lq3eeMpcFV90bnb83xfvYeTpTpjdDgKY7zXgpqvBGJKZbZd"
    "lTV0gw3CrVLdYc/tK3eeL4q//E7tsO795a7hRufWqTm9EwbuJdnSHnXHZu7HtJtjjXbViVesJLJZ"
    "HHSXVMi1jdDHcNyjjdLlrtiSack33L5dtMYCyH4Qwjc6Uo22ZN5IvjVrxso3Xx1JW97Z26+bE5vL"
    "G5Vu7Fp1wLDpzb6WUIl5JUt+oqnkufkFQJRLaixQ3nCTYprHBVUhUQlRF1hvDUmbGhb4J60i5FVF"
    "2xVwyNfzlrFxqpL1wi3lciURawtpKldYsFw0D0UKKEZKla0VemE4S8HTPSKOOmabCOEuVVppF6OG"
    "h+ldnCETpqSdN0IikqomiRwyUvXCVJVppnpBKLpoS6rdrBJiHQsl4UWoqoK6pFuK5ZsvWEqSqiad"
    "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"
    "AAAAAAAAAABDBDCCDBBAACCCABDDDDDBCDDDAACAAAAAALFLBCOPDLCHLAKAGKMGMJFELDKLIGFK"
    "BNBKFGLPGAPADFHDPLLCEJKEGEMHIPEAENNIMFBOOAKLBFADOIKIONLOJLNMHLPGPHFOELIIAIAB"
    "NBEAHAAJFFEIIIHAFGLICCBGNJCLHCHIBDGBBPHPLJBILAAEJAAEAMALNMOMEKIFCOGAFIBBEFGM"
    "MFDCOFGMNEDBCGAMINBDDDHGIKAJFADKHHMCNHAPKLHIOLICJOBKCLFBLJFBALAAGKIFJIGLKKHF"
    "LLBLDFADMCAAPNNOGIKFMLGLLOOKJBJNMOKKBNDKBOHBHNAFFMENMFLIFCAECKDFBIKJFALOFEKL"
    "LFCHJPPPMKADJNGJOEDEJPOMHMKIAKHOIKGHJFLJAANLCBBEAGLBOPAJFPGELLIJHAIIJLCHCKNK"
    "OJFGBJGHDMBCCJHJMDDNCCONHPCLGAEOLJBHIIEGLCCBBFKFOHHKFJGBPEALBMNPCPMPMCIGECOI"
    "IAINFOLPONFBKAOBMFOBKDBCNHLMPMPDMMEADCNLGCDAKBPKAFJLAIKFPMEKIAHLDDGAPKODPGLJ"
    "LMPMGIPAMNNNFDOALBNDFHAJIGHJCKBBIPIPJCBBHHBNOOGDAKGFFKJDGDFCDFLIDPNEOPMFIAIB"
    "DDOOPNPDJLOINPGNJMJKKPMFPCHFMPIDDIDLEKPLFAMDIOPIJLGCDIHGANNEACNMAJJBLOOKGEGF"
    "CAIICMHPPAKDENCFNPLGALNOBKGAMEKLFKBIODPBIFENIONFCIIFBKHLINJFPFHJACEHILACGLAL"
    "DMNFBIHBFPJCBDMNDPCLCEPLAMAABLLGMAAKCGGDLCNPHCBABFDJGNNFBPIOELEEKKBFEOACFKIM"
    "DAADDFAADLKDKJLLFFCCOMCCGLJCNDFLHIFPIPJMEEBJAACFHLBJADNHENIMPKBNIGCPKNEEKIKG"
    "CFNFJFNCABHPJPNBLMEAODMJDKBJADADJIKIBBKCOIMFBHPFBMEJODMPKFAELOPBFJBHEAGIGDBK"
    "OCIBINADJKCIPBAINIMJOAICHGJPCGCCALLOINDNEOCOPABEAOKOCAAMJBFFGDBCFPMDHHCCBLJK"
    "BDFKINFIKDJJKFMCNIBJNAOJGBIHJHIKAJICDDBOAECIBPAAJIBPLGKCJMHCKEGMABDKONKOMJDD"
    "KFHIMEJGNNGKBJCBNPAFIJAAGMDMODJMPDOBIKDBIFLMENKFKPFLPDIACBJNJIHCIHNLFPEDLNBO"
    "JGPDAPKHHAFCHMMJILOOFDBMAOBDDECMIKPAKAHKAELIPGENJBLENAIHPELNPAFECFHMAHHJLJBE"
    "MBNDDAKGGECFMANPBEKAEBBIADHLJEABKOEAHINGIIGBECEHBNIBOMNBPLLODHPJOJIAJNECLNOL"
    "MHLBFMADHIAGNGCECNGDJJPIHPJIMAOLACHIHPIEMEIMOIAGDODALFHGAAMECKOBCFJPNLKEIOCC"
    "BBIMLPBBEIOJMCJOBIBKACKGNJJNFAMBDDGABDBEKDGHLEMLONNHEAOKNLLBCKEPJPFAHDKELFED"
    "GJCCEDKHCKJIADOBPCDNJJFJMLMOOHFONNODACNKBENMDMMHDMPMEGHOAPFFNBLGHEPAFEIMDHMA"
    "APCOMBOFMGABJAPNNKOEDBNCIPPBIBCFMALBIIMMBAEBIOAIMEHIECHJGDOHKIJKEAAAHMJANFPO"
    "AAJBDGBFEGKDOHOKJNDBMILDNFBCDONNLMNKCAAAIBJHEKHBCABOKBMNEJACKBPEOGBEBHHMKLNA"
    "IAMICCLKNCHBHGNCCMMIBIMECCMIAIHDDMAAAEAEKAPKMEABGEAEEJGEFAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAFEKAAAAAAAACDAAAAAAAABGAAAAAAAAAAAAABCAAAAAAAAAOFAAAAAAAAEGAAAAAAAAAA"
    "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"
    "/9k="
)
RULES_PHOTO_B64 = (
    "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"
    "gWvFlBvLGe2pRIL3CIETIzipi1txKekrrKupZ/tex9rY+2VtxZnVRYVzKesZFu7xa9oEpqJtrwzs"
    "61UxKrrI5q3FbiNn6CulUVHL4DZGyvm62slXHoNQrVx2W1fNHUxHHXo/4lzfR6RFPdR7pnlW4lsu"
    "exsJ9FaS7iJDgbPuWU9zRZmSG7WosmF7x7I2bK5NC/8A8qtLP2sXLlYpqxl5uKFTKpuyv/H1oStH"
    "staGquCwt1xLLdTTRrMm/wD7fbR1y2KHaOfFXLOyhu7UtMtMWs5mIjZtlB7P2fEN1m0UGxYsH9o7"
    "Nqtra6xrINVAkNObEOsrmUC1pgWLFrGmToEN5qviympsf8KROjRDsto4deLCht9oT2YoV0jPLtTQ"
    "Sbo4LCosPmuZcSHAjRlwnGHm5DOq4rDqltpWlbaFp3EmsyyTcVlg3WGnyJpBKQhLadVJJaSIkl6D"
    "fprbS4lqoaTZTqZmUHWGnyNpBtrbQ4lbLbiEIS2mdV8bY8DFEOpKLPNtBoNCVIaZbZShCW0ttoaT"
    "+FthWTVW+y70SnGc8+1tdZTxBbdah8zlA5PuXmG5DNLSv00rRxe43CklMiJktLNh5EhnQzwSbFpU"
    "eS9w0ZmT62kGWU1nlrphWEP/AKGXCjz2r/jtmRsZayrKNy7W+7iD6/B9uT/HZjIh18Ssi+o3HbTS"
    "pRwVjXNJhTZ7ZMwkJJ+1djtzK+Os0zYDJQ3q5bi5NrHVLr4s9M846VRbF6OlFEiO3YW8Zbq61uGt"
    "mHs+6uUJcZuZtEThlUw/4n5suDHntxojEJr8JSSURwI5tJhMJlSaRlTUevjRExqqHDcarmGXY8ZE"
    "Yn6mFJdTEZStmphRnjr4yo6mPQFdAKHBZpoMZZwI6o0mtiTHDrYqoqauIhl2DHfUUVlL71PBkupS"
    "SE//AJj84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR8"
    "4pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR8"
    "4pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR84pR8"
    "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"
    "Slapxk8JOVWiLmS/CsIgpRmRbon4y+g/9UaZmppS4y2TwotyphoKZJ/G+JGp2D/JirXdzM4kVmQK"
    "9xaDNsv3C+qovORd8NNTtW5HjL2FRcnXb1537k6ofbZJRdpUFT2kzSJvSZE4EuxK2JauoqXHTr1J"
    "GiVBlJdJi6v5Spk4NleVl2RNz1TWabfcVs715HjdkaQSeuLi+GjI3KNqKNbvj+yNBSzaI809i3iT"
    "it301JX+NUaRCeIJSVyNpAevVlab/gtIfmp1fy3IDbVKYY0y+Ov/ALQy0/cTaSanbcqJW9I/2hUi"
    "WqPTFM/uwx/Zj+z07DnGReDcUI1Kti02mwfQ1RdSww1hJhy6iTaeyqaoOZFtEfMbSLfEszLtALAT"
    "OKYLqpaqQYy7IijnP23dsYkuwIHqRd3Zuho2htwRIQHYly1WHLKqrhqZKu1VgnX5cDMkoXXAGIIh"
    "A3hiu4d3dGKzLiBpq+72boVhWkwlK6nXWtfjD7smyCzDtFK4qXe+EYctIiqTlEyVV1wBsywiYLUV"
    "3QcsTaKwaqpDvqtYFyZZEzHKvVugZVWBwBzQd0YKMJh3IapvXr3w4TrSErreGfWMC8gIjghhov3d"
    "0G6/LCZnzuuEEckTJP65edfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnX"
    "yz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R518s/"
    "pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R5"
    "18s/pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+kedfL"
    "P6R518s/pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+k"
    "edfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnX"
    "yz+kedfLP6R518s/pHnXyz+kedfLP6R518s/pHnXyz+n/PZKpr1fYwNMCpuGtBFNsKzONEy6mwvD"
    "RBQJfTjIZJzRmRT9/wDHYM9pYEd0mafksqvqffL+P9Decpe4SktEp6OWk22WjmTcsaN/o2hTWf7o"
    "WWbF7/aV31vEiLadiIkTM3N6Jb0S+06IMKA24i7UpTdEtKKtqOnyl6taw9J6K0bJDLsErdXWriJU"
    "21iamkSX0fLNihPFqAOxIbnJadCc0criA482HKb7RhzFmUTRwMY/HLMlHs39US+kXZ9uXlnCJCVw"
    "ObnT3rEobc0y/JzZUameaP8Ae3Q+5ozSjE68wCuONIKjyerfGjpzFv44h8i3m2rEnIcbpxmWx78L"
    "m68qV6onZnEwWJYVoVtb1TZBzIaZEcFsTfTiy+Lr740m/JTnGW5LDtVGqYl37IoSUWGNK41cV7Cw"
    "7NWvOvujRR8abTj7d9XEtFvVrX3xMTOi9JNTyyw3PAgKNE6t8SzM7Qmlqtq+stNUHJzuiJaWlhup"
    "+S01as4PSeL/AErAw7eqtaxo6cxb+OIfItpbasLo9NNDxxEqrfFl3V3w9MzEy3JyLJWm8aVz3Ika"
    "PBmaB+UnitamAH9qRpM5qYwZSRMm8WyuIaLqRIZmdLaQa0cEwlWRUFMiTsg2gdB8R1ONrkUaFJiV"
    "lXCfBy5XWrtS/wCsaAdRtpk5lq48MKJVbYc0Sy7ejdFN6zUlEXV74n2ymW2pWSKjsw4lE+ESkpJa"
    "TamQmFJLxbWoUSuYxPzM0/gS0s4rQFZVXT3IngMHIIqzIn4tEStVhwtMDhzKJS2lKJ4YaQ0mKOaT"
    "cSsrKr6n3y/j/T1WNOtD2DMD9f47CbcFQMVoSLs9H0boiQA29DgAq48Oo96/xtjCGYBllrmScpz3"
    "F3uH/HvjEmaCA9G2OoYlpsUuwiqqb02w5O6O0vJtNvlebcyeGQKuvtjS2hSn20bmLFZmlCgqSbFh"
    "7RIT0tNTc88NxNueKbGuu73Q9/s8E0LbTDSYM0blBNxM1938bo0ew9NNSpcYOwnF5CrUsqxofREz"
    "MNzjLT+LMuBmCa8uzld0aUF7SWjcJyXcSWal0BEROsqa+qNDNyiyrzlHMUTFDUOVl2RoyZKal0bS"
    "QoZ4iWiXKyielmHQZ0fL6Pdal7ypeSqlV7VjT4OvNtm6yKNiRUUudqjSzbM9LyM05h4RvOIO+saN"
    "bOZZnZ9oS4w+zqXdntiUGXOWcmeMrc05Q1QeVnbH+zR6RclzaRsuMAOoCtSlUTVnGm2ZjSOjzV2X"
    "PAblrRREouVdq9UNJNTISjIcs3Cct1buuCxZkZbRqAqAjhUTtVV2w6jBS5zCzy+LcRC5Nuukf7Os"
    "i7Lo941XgDKxKpsTVkkOaU/lPRqsuckQGaRT5tNUT2hXJ5iRn25jEBxwUJsqimVSSNEhpHS8nNtg"
    "/euBagN9d1E64npQn2JV+UmDcl1vtbfGvwr/ABviReZ0hLScywwLLrUydiZbUgZCTfbevNAF3UKx"
    "JSsvpXRjbUizhUdmLSv9bL3Rod9ialn2tHoLLxNuXJdydUS7ej321SZmWnJx9DS2g0RBruyrGndF"
    "uzbbCTEzjMPqvIJcslX3Ro43tJSbyXGpk07UQS3KpQ+GMzKzcg6atDegtvgq7Ov+NvgMOSCKsyJe"
    "LRErVYcLTA2TKIiKNKWp4d77hOnSlSWsCbRKBitUJNkKRrVVzVV9HwgmXhb9hHFp5BjRxIGAyamO"
    "XKrn9fLo/JuYTqZIVKwJitCFapBzE0eI8fOKngNTLFFcaK4btUPPu8901Mqb1h7Rw2YDrl5ZZ1/h"
    "PINTMstHW1qMLNTijfSiIKZIn/yjOj70jo+9I6PvSOj70jo+9I6PvSOj70jmd6R0fekdH3pHR96R"
    "0fekdH+skdH+skdF+skdF+skdH+skdH+skdH3pHR96R0fekdH+skdH+skdH+skdH+skdH+skdH+s"
    "kdH3pHR96RzO9I6P9ZI6P9ZI6P8AWSOj70jo+9I6P9ZI6P8AWSOj/WSOj/WSM2/1kjmd6R0fekdH"
    "3pHR96R0f6yR0fekdH3pGbf6yR0fekdH3pHR/rJHR96R0fekdH3pHR96RRxKL5FCEMl646PvSOj7"
    "0jo+9I6PvSOj70jo+9I6PvT7Bz8vlwdfhD+HyLfZ9gb/AELLyo/h8i32en9foGrw8vIj+HyLfZ9v"
    "j+HyLfZ5TP0XP04fw+Rb7Pt8fw8BuSTSKArS4lpHRN/4kdE3/iR0Tf8AiQLs8jYCS2pQ6qvC32eR"
    "r9rD+GGpaXSpuLTsgRbRFUUtbH2yjJmV/wAMvrHQS3+GX1gJjS+G04SX0FKWjBGKFxdvksp1b4zT"
    "gb7Pt8fwwbUwKI4+Nrbm7q98SasKrjLvi0TY2u2GQmFQGh5NbaqRQZylSEVoqq1SMGadW+laIFYB"
    "8QtAhuS4aZQoy9ElWeS3T1uvga/D9vj+GEIFoSZosXukiTDOT3/7QEvJcppCw2U/aUVvrhpQUpzz"
    "g5yd5TDZXGq+sW6P5PlSo88nLp6ocLf4fszL0Mfw8DqS7hN4o2HTakLpCaGjzycivqhDcpI8tkSs"
    "aTeu0o6mk/TOH35siwuc6adyJD0q/wA9tfjwN9nomX2WP4eBMRPyVnlOdfVCSejJd1xx5KErYc0I"
    "KenW1GZPIBL1BgJaVlnuLNFaHIyIvahsXztFFS8/aJYTSEqlzzKcqnrBwN9nlMvtMfw8DcsEvMKW"
    "twqJyi+MOBKy74o2lSM6UgGXbjdcStA2JFxMTCr2J9Yl2JJs0Z1NgutSXbA8dcxUYatWvrLugyQU"
    "G5a0TZDdNdPJ0T7UH8PAIpTNaZw3Ltz0sp63CxE5RQcxNvS7jp61WZX6xzpb/Mr9YSYk3ZUHk1Fj"
    "1/fCS0qaLKMbU9ct/A32eFTwK+mZekj+HyLfZ9voSFTKOkT4R0ifCOkT4R0qfCOlT4R0qfCOkT4R"
    "0qfCBCtaf1Ey/qNl9ha+GVE0QhU9SxgHo4HOTWqIkf7qH4DE5MsyTbKiJInJTdA9sM36PbcxK6hS"
    "LXtGIIrtQUgdIaLWsuWsd3Ay5pAAJyaOg3j8IdaTmc4OzgCcelG3yHXVNfKj/c7Xd9Icf0e1xZ9v"
    "Ym/gkVZaBtSrW0aVySKRfPNi4+5naaVtSH0TJEcL9sTSzDYuIIpzkrH+6Q+Aw6IaObbw0rmKQ5+J"
    "YQRzVckgNHOgHGSZVbrc+2DacyMFov2bnEn/AGiR/wCkPBP/AN//AKYHtiS/vfujKDCa5JGnJFet"
    "YaY9WtT7ICXYWgSu72oYn2+kbTl/v4BYYpeW/wDHGtj9P/SHyJFffd9gch4NH+/9iQWk5/kst5hX"
    "9sTLi1RoW6NjuSsTH9oX7Y0iX3R/fwTf4Ehz8Swsw50UulffHHg5onQU+7DM+xm2+mfbw5+m08tJ"
    "/wBpH/pDwT/9/wD6YHthjjkok1fW3VlFzGixQ/ckJjUFseaCaod0hNrYh5ItNkEZzUwpEtV/ikHJ"
    "ST5uI96hw6wXqrl2RUVov/8AcdKf6UPS7xk6xh1oWdImm2uYLhIkaPQ8mW83O6EkpPKWayy9ZYmP"
    "7P8AfEx/aF+2NIzJZXIqD8P9eCb/AAJDv41htifcVkphOWo66x5y/wDx7odkJB0ncIeTfrTdCiWS"
    "p4GX2EvhsPmiqLZ1WkY7KEI2InK4JmTMDV1y6ipqzSEWJfAAxw7q3cCXaol5WSA22W9aF3cAOt84"
    "CrDTrAOA6iUO7bASM6w66nrU1a674/3c9/H96Fb0PJ4F3rFCkWarCMSQuNmqUIi3cDrj4mSEFOTB"
    "PTzauS5KS20gZPRzSssbapT3cD5PiZYg0S2AemRImUO6iQhghC0A0BC4BeWqhShIm6HH5QSAXM1Q"
    "t/kN3BRPRF+y8uHlf1Eyy4ar5TP0mtMk4KEiovllURVUTXCiaKKpsXwMvI5fZU9xhKsnhifVrzh4"
    "propbNfv+ynvh8T2tXbs7Iaxg8UTiDcJIX7IebHUBkifGJYpZl1xHGkIlQa51WBao4iDLo66lKlD"
    "xtS0zKk0N3jcxJPhrhlp2thFRaRgpLOgqoXKx66kruhZqavVUdsQBWl2UA7IIYeNRo2zWtFXVnHF"
    "ixlVFtV+7b+HdE9x0Cc4tRKCdvrUiWfl2zbxCJCRTu1U+sEISk2zyFIXT1e/KBeNmZmMbUDOSUTf"
    "EmjmMjU0upznpvjBbLxJcsXF/N74daYqrY0tr2RJuMCXjLsQ15o0WDFpp+YBE5KBrLrhp8Gnpe8l"
    "FW3YZkyZdUitqeLvTdSJhwOkBwRSpoiUzhzjoEgNtK7QfX7Fh12XZNo2zFOU5drr1dUBxZh4wJsC"
    "rbXWMOMhWg019kcYmWjdInbEQXLdnZEo0AmLLzYmqXVXVF82qg2VyNomsyRI4xnfj2e62JV7OrhG"
    "he6kKISs01yVUXT1e/KCeRV4yhrQfaFESv7YfmHVW+lWh6q0VfAz4c/TZllUWrtlPdDLDi8hrV19"
    "sOTBCaNm3Zlr5tsKEsLpXmBGp01CtcodcTUZqUS6NqQYbSAufWsA5apBgIy4KrzoVGOMqa/nCSiQ"
    "08SKqAtcoR4kVU5XeipCjNARAUzrDWnJgGZATEUcRxTPnEqao4ybDuLW5W0JLLoNZonSemyvcw6Z"
    "UVcoa4vffKu3ILtOXX/tBzKDMEZ3ZESUSqQDE5i0b5htLn2Qy80LuE2teWVVWFlsra871qez2Qbo"
    "oqIVNfZEmMuqibF1dy1WJgcNxtmYRK2LmC9XVDMvY9hC6pESqly5QM4QLah1t6oeYmMRLzE0UOqv"
    "1hjBByyXErKrziXf1Q6y+tykYkmWqlfrAk0pgiNiNK7kpBvCiohU19kMMS6FaFSJS2kv/aJN8gJR"
    "YbESTfAzBhRsRUQAfVSkLLTSPJ43ERW6bqQCy990q5cgu05d3/aDmEGYIyuyUkolYZEMnG3lcr7k"
    "+kTKmNuICAAjqHNIbRlVXxYqdfajL7Mps8tyAIk6kigIpFuSLSRRXr8DPgX7OmciI22rgQd9Uiad"
    "mGnW1btsuGm2G0flpp8zFCVWtQ192cE1WupUWlMlhl6ZaddN0yTkuW6qdXXBNApNy4tI6SrmqJai"
    "w4MqLzTwgpjedyFT3Q1xwXXXXQvtA7UEYRjDfNDLI70HZupD2E240jbBHm5dn8IfemCUXMLEaDqr"
    "rWJVx9l103rua7bqXsjjDSENX1CilXKkSIJWjzYEXvWNI6/ydaB+lSHXnyVJkbVRv2RVdsSz7Cqt"
    "wJiovqquqGHFVccjVDT2cqwyAGSARZjXXlE1hPJKniD44ltr92sS2K6k0d5eOFbkRPZrDqzCEoNt"
    "EdBWkY0q242eMjdCcu2dkcXOXnDLUTwpyUXspqh9iZZcV1gTUiR3Jae6PEATYbiK7wcvJ5+nzKcq"
    "51q1FTtSJllxTNXbbc9VFgON8YbdAbatLkVIIgQkHZctVhqWVyYUWE1gIqiqvvhmZl6uA81hOtnr"
    "oiUg1kheJ0xUauU5CLDQzwui40NiG3TlDAPg2QsivNrVdUGbzeKJNkNsTZzFSJ9qxKbM0iUaRFQm"
    "UKvvWOLzWMio7fVtE3RLH+VJgCIpyRzp74nnFaxMZaghakW6ucTgvXG4/at3YsMk2CEgso2YHqWE"
    "bOqu4xOEXaiQ08aKoiuyOLTYnYh3gbetI4tJidl95G5rVYmMiV50bOpBjASt+MjlfdGM/wAZB1ec"
    "LZclV/dDyki8tog+PpafZOUL9hJ9h5xT0xPs1fL7/KJ4C+Tqmr7Xz4E8BfKVHwhF1zBBdZ0rSGCb"
    "fR8HguEkG3gFpobjJaIkKyLyOqPOVEyrC0zpCrRaJHIAi7EjJM4zbP8ARjkCpdiRSmcVIDROtIoK"
    "KS9UUVFrFtq3bqRQQJV3Uii645IqscuowjfGkZeIqCCtqtYIa6lpCVSlYRLVqvVHLFR7YqjZqn4Y"
    "VLVqmynBUWyX+7C5LRNcVotPDr5VPQaj4Wiv/L/vi5mYdJ72Vaonxg3WpuXbnXeSmIVMMfrEq62+"
    "xeokpUNfGZ7InGJh1GUmGrUNdSLGlEGZafJcOuFs5USQMzjsm2rSEmGzW9dudYkzl0WuSu1C2pb6"
    "RpIXnzcb4s4qIqxJFIEraO3K6Y61LdGh5mYSky47RV9oa64mGQnnJgyvRJdwVtrurAui+csZuqLj"
    "jbdy9nVEl4xx+YFzpSasuGNIzEmeNPDmgKNLN6pvhHnZ85Vt1xbbAqpLEm9W4nGeUajbd1w5h6RS"
    "Rz9mt0Ah6RSdz1WUpEr+L90OfiWND8ad5WEtjapkee+J599/iatDR1UCqjuRIVzjBzatvogm4Fqp"
    "lqjRrDDxtpgCpIK71jSd9bcA60/CkHNS+NSWIVdByi1GsMEM82ko4SEDWJatn4Y0yMwatN3hUkG7"
    "1ljSHFJgn+U3WrdtM/Bz8DP02o+C0DpVFobQ6k4Wm3CqDKUBN3A6yBUbdpem+kYbTvi/ZIUKkPHi"
    "8t5KGVM4cwipiBYXZCjLuUBc7VSqQMw44pOjzVXZBeMQVLWQtiiwXF3LULWmtFgDeOqhzeqONYlH"
    "61ug0bNLSK61QRUrAcZPEtySvCDzJWuDqWFVdaw0BlUWkoHVGNjcu21ck5SdcIJuIoISEg2JSsYz"
    "53Ob4OaFzxxpQitTOLHHEw61tEEFISaVyr6LVCh5pC5D3SJvh1kFo25S5Ozya+R1+h5fY2XgZ+Ds"
    "9Ky18OfD+UDe20BOKPtU2QgutslLVzZw0pSHmWuZrH358DU20AlMzBqgmSVsFImm50RV5ppXW3UG"
    "i5bOGWflwFZmZUvGElbUTdE3xsR4xLhiA6I0XsXgYZ1YhoMOS8iDbLDRWW2It3bEpNsAjSTTdygm"
    "pC2+G8yrJq3MtKKHZkha0ie42DziICNA2re1dsKLgqJJsWJx863tqCB7/RdfkNXl+vwUflAG0Oep"
    "rQYx2ybJ3WjKvpZWCenQoTuaKmacCy2lRbbliW8FNy0xXqh1jQmG+byUcNXkvpuRIUHEUSTWi8CS"
    "umMNlmt7Sk7a4MOS2hMN7F6U8ZFNU3UhRNKEmyBSTBScHldkI/PutMzP84LT6UJYB1WgSTbSxvCO"
    "4RTw2ZQpdX6FRu14m9fZAutNK+F1h2zh8k90G423cbi+rqSAkWDRwrr3jHUpbuBUTwc/sb73gSUk"
    "GQCyhmm8i4J+WPPDDGb+7TXDWIlQbqap2Qbzq3ESxUFoqbUiRnV50wzy+shjFPMZdsnfhBOvLcZZ"
    "qqwhtraSalSJWaRPOWBIvxbYbw+Sc26txfdHZwcUNfEzIqBj7oIV2LSBRSQKrrXZH+9pXvi5qeYm"
    "FrzQ18ExMTAYgS4XWb12RLt38gjTkA2mqHDwpRxeM+oCGNOvrixXLW/YBLU7vRc/SvvcCRx1ijso"
    "oiiGC6stvBMzU3RqWclyAVL1l6ofEjFs3JcgbuyS5YVqZBQOBbaFTMtSJEhKvGPGWlK4EWtqLE41"
    "eIOusKDdy0qsE1MArbg7FgW2RUzLUiRo6XcIVmGRJHEFa0ziQ4lR3iwliinOH3cDU0iIDDB1Nwlo"
    "iRME0tQJwlRYHERVCudI80mf8VIpJMPNuV1mdU4CVQxWjGxwN6Q+9ocVxxpnN80R25wYPN6OWVVP"
    "HoxmVPdC26vI19PVeDPws9fBpJjYUvf7x4JZnY1LglODRcwudWibX+6sYn5poz7oUizVeDRswv8A"
    "OS1q9oxMvasGWcLgl0rQT5K+9IcD2SVIkG0yR03DX9nhjJBkyyKZe0VM1hHGTVsx1KkMTAjbxhpD"
    "JE9rb9kU8jnrgQcWguiTfxSBE5YkG7MtkTBNrUK2p7suAWpYcR1iYVbU12qkT8zOtqzVnCC7apcC"
    "1iV4oGK7LukJCmuixPuTjask6CNNou3PPgYdrSxxFh9yVYJxlwrhIdtYkZP1pdnl9RLr8J1ypA23"
    "7IXqq9kC5bOidiIf5IvKVNuuOfNJ/wDBr9YbDFeuALQulVFPjCoutPsjOMvI0xXKbr+CsVAlFepY"
    "8YZH+Ja8PIJRXqWPGGR9q8NBdMU3IUVXX4XIIh7FjpT/AEo6Zz9NY6Zz9NfsyvBXya/1Dz8LL0XX"
    "9nUhU8GvomXlNUavScvI3fYlf6l19JnJiaaJ5GUGgiVutYHissUuqa7nLuA5yeuJu+wGw1mUDxaW"
    "KXL1quXQ68I1Bql3vgphB8UJWqvXAuA2iIXMuNEu7I4sgUe1WrlBvEgWN861xFjFbBEb2EZINYGU"
    "JpRfLUKwZIIOWZlY4hKkXsiiAmV5FakDLmFrpak3wbIj4xut2eqkW7dUdEn+IP1i09i0WkBKtyz6"
    "3rajyuZ/ow6zW7DNRrDJGOTyVDrhZexMURuJLk5PbAE8KWFqISqkNuigILiVG5xEh2Xs8a0KkaV2"
    "RRM4RMMRJfVJwaw+4g8lmiH1Q6+I1aapevbAmADy0qKKaIqwQOIokOSovouWXojruiiWlaGgKlfh"
    "DK6UREnsXk+1ZTbGGkmKOW0xL1g2dHWrONu3ddq7ob/lXKdxeRXn2dcTDE2Bmy/Sti5pSHeJC4gc"
    "ZGuIua5Q2/KaP442QjaYvKlvV1Q/OOtI0Eu2SOKh3JdbTXGlvwB/1RJHo8cZptlAUB9QtsaGYcJD"
    "mGUo4qZ06onFWQckrmz/ACgj1fGJRGJXjhM3IYI5aqLvpGjGHpdJfCLIcS5aKsT8pJBgTN6qvKrj"
    "ImyOWnbHmkx/jJ9I5CZVySEcYlienjTJfVZT6wQzCKLic5CjRCzTNSweS7dzM90TiTiLMvCNzYIV"
    "Mau2FbCUWXMpkaNXXLEhLlz2WQQ+paxpGwbywT5NNeSQcxOSLUo+yYKxaNqmtdVIYMjeGbM0Mm6I"
    "SCW6saYWYaxgvDk3W+ssaQ4rLcWoTdfGXVziUViSKbEgEuM4tERf3RN/j/d5Ovk6+h1ElTsipKqr"
    "wVHJYqaqvbwU2RySVIpVaRr1xkqp2RrjMlX3wlqqnZFa5xdVfA55fGM1rCbYTlLVI5y/GKrmsXXL"
    "dvjMlWK1zhc4pFLlt3Rnn5Kq+Tp6EINCpmWpEjEfljEN/CvFWScprVItmWibXr4VWVYJxE2xZMtk"
    "2f3uHE4q5ZGfBlmsYhSjiBw2SrZOl1QizLJN13+HSHGwZ5beRoqolIVqYBQNNnDayKmqJWkI60ze"
    "BZpyk8jn6dpN5jzkGks6kryoYESI0cKjiLnUdsPg1zBcJB4NGtS6qMsTN3J9Y9sTKzSqSMujgqW/"
    "anDJMsqoy3FxIbdq7YvmeUrT9GiXX1pwNX0WiKoIu0qZRi4jmPdv2wduSkIqabipwEtEVwWiJpPv"
    "QLgOuLMX79cTAtaq1om+nAwUtVBddLGJO5I0g0+qlLoxfyvVLZ4eG1ai0u5S0yiWdOblEWwUf8cm"
    "z/SHp29k2VO1MNy6m79kMN0rc4KUiZVpEQMRaUgJ0DabbacoWIdteqJh8JuUtQSwPHJrXVAI8QFe"
    "NyKBVSJxx1EUG5Y9e/Z4efpzU1OzaypmlQbAblIeuCGTIpIz5zgMZwBi4j8u7m26O2AZYSplCys3"
    "MrPAnq4VRFeqAYkpzAp0TBNWjWDBxLTFaKkIyxr11XUib44rNTBTwDqTB5I9kI3JTlij0UuTdgwQ"
    "mlCTJUhJ2ZmOKMiXJKlSVeqLs+Nfn+LpXthZ2XmOONKXLJUoQr18AT01MrKN18WqJUi7IUm6hM/n"
    "+LpXtjjjD/GmDLM6UJC64RttLiJaIkGzPTeMpdKwDdwxxWVmFkm19XBoir1wTLyUIe/hpwo/o9s6"
    "j66aoFJ5HFdfVRRRbCjXWsYTwOEAr7FiQSg4MxPklEs5rX1XgUpJowFzfTP3LBg0jvGGERVcwRo7"
    "Xd2RjTvi02uPclEjiejyxAVauvL66/SM41xr4NXkqeist+2aJEzuErU93BMJtl5gS+OUaUmUyUGL"
    "E/vLwIqa0hHPzzQOd0aUeHXaLfxXPgBwciFaw8qanLTT3pGj5f2JZC95cE/L7HJZV96QsSsv6rUu"
    "Ke/g0kx/wb0/uxifmWic7oUizVc+DRbxZqTKhX8K8H5a1ME5X+bJKR0M5+mMFh1QK5V4OLDlLtCi"
    "NDspTXEgLyzDRXKdrYKqKi+1DrM2Z3CWpSqnAzcl1Kqg71plFFucfcKJ5uWcmcXDGgqKiCW67Vjx"
    "pES/eX7FYfNLkbNCpC6QlXsVh5zNFShAWunBNSbjmNNzFtwjqb2xNSU0ZM8Yto5SqJTfDjDtFINq"
    "QQ4gtA2F5mWxIYSVuVthpG7i9akPaPmDVgnnEIXKVTsWHGHec2tMocvcRllobnDXYkAUvdY22jdS"
    "1rTbBT0o5Q2GhR1ktidXAE9NuVJ4FwmQ1qnXCLDukpR3moOKyWsdnAT8y5c9MMKgMj7K7Vh7jNyA"
    "80rdw7KwKXi62YXgabUhthqiEe1YlpGVNX+LqVXaURa7uDDBiXcStam3VY81kv8AAgiWnKWtE4GG"
    "TlmJrDzQnPUHt3QVulZ2ir6iZJEobJrMsCNuOutV3LwCYLQhWqLD8xNNtSoW2nNAlpXdXXCIul50"
    "UrrJMofJxtG7yuSmpU3/AGNpANrLgOJ+yJYPadH9sTZf8ReCXe/PSwFGlnfuAHxLgrGLseaBxPhG"
    "kj9omx4Jhn89LGPBJtJ/NyoJwaQY/OSxd3ALf5tkA7uDRjm4Tb+Cw85+al3C7vDnTDnE6AF+HPg0"
    "i0fR4N/95Fy4dHNj0eDf/eVc+CTNzni4YD+HL0Kvok0xMuYQTDVt+5YCad0iw6LXKsDWsGa6yKvB"
    "Jgc23LPS4qBYm1NkFKS8yEy466hGoakROGVUpxuXeZawyRyAkZd8ZgydxHDDVq1cDDznMFeV2Rd/"
    "Kkvh11baQ8610eoexOBtx5aN5iXvSBM9Jy+GhV66Q++PNMsuzg4m++Mu429iCR6lSkTdk41MvPhh"
    "ijezf4QC4VgEWZboeZleJqy5kuJMFyqe6ObI/wCaP6QvIlc//bC+kNLJOCSEPLATutXtguPGggI1"
    "RFWly7qw20qSGG3WysweXdGYSP8Amz+kIhpKoP8A5wsv1YdCXcxWhLklvT7bpwJ4Vft1P6kU9Cz/"
    "AKhZ/wBQk8Oi+VX03JKxmnBln6BqXwE+0eLyviyxL7/dqiUlpo733PHOdXspF+Edmu63KFXRKVm8"
    "XxtE5VuyGZmeREnRdsPfb1xOPKq1lxFUTfVYmZq5UJoxRE7YbTSDkxjODdRluqB2w3JunVtzMTTa"
    "NImSlXJi9kLuWKUhpzST7gk8lQbaGq271iUAHcSXmuY5TOH25c5gTaElq4CW5QMzPE6gOLQBaC5V"
    "64lMEyKXmSoikNCTOJrHIm5SXVUVyma7kSNtsedTP+En1gsNVUa5Vh5038ZW0o2xUUUyh1ZjJ1S5"
    "fbEgstcZzIqq7kzgmlmHVZFOeIVvLqjjUmbtgnaQujRYlXJxyYQ30VaNii7YfkwM1QG7hXauUCjj"
    "Jt3raikNISScmJkpi61VEEtrGkSQirLUtTfyqROTDlwEwg2pTXVYDEemVcLXY1yRh1gluUF1xn6F"
    "u9GaWcK1lFqWUOvl6y5dkYPGHMKlLLsoVXpxZQ01eLUq/CG5KTUjaErzcJOeUKcuVLkoSKlUVImW"
    "ZhRRwjFQEAonXDRFpB+RcEEExQVJF60pDs27MvO4Q0YQ05R5ROg8VCdatDthgZp4pZ9kLLrLkIY0"
    "eLVySsovOJM13rDpPaQObZK78nwlz+MBLOTLsmTSrYYJVFTcsSn5W9Mg0aERGnXsSJpiecI5F0lw"
    "ypm3uVI5C1ouSx0yf4Y/SFJz1lqsA8M+4ICt1mDy/jD0wg24hVpGjwlXFVGgVHm11LnEwss4TQvt"
    "+LctzZXdDksc65MOE6K3kK0pEs1KkptS7dlypS5dsPP4iiybVt1q+zD7QTZTJvWonJVEDPXEuSA0"
    "42FuJMG1yy640mTTituvKmEqfiieZnZg3DNBwkXtziXVuddlAbBBJkA1rD7zC3AVKL7uDPw6eVp/"
    "VIF0hOJLGaVRtAuWnXAEhi8w5m24O2KwjLNK61VdSJvjClNIi5MeqJN2oXvggNKEK0VODjM9MJKs"
    "EtA5NxH2JHGZJ9JqXrQlpaor1pwhMaQmUlQc6NLLiKAmJZ4ZqWNaXolKLuVOAGWUuM1okYK6SHjG"
    "ro1tr+KDZfGhhr4DfmHklpYMlcVK57kg39HTXGhb6QbLSRN/CsxNvpKy9aIVKqS9SQszITCTTQZH"
    "ybSH3cAiiVVdUI3O6QRl/aIt3IPasYTlFyqJDqJN/Aa3iyy2lXHC1DB/ybOcYdBK4ZN2qqdXhm2J"
    "2nhkQp7SpshUbmXEdFtTJFaySnXXgcVsao2Nx9ScD0xfTBMbxp6q7YdmZWYM8NRShtW1r74pGPb4"
    "q+27r4JaYeeJGXbkK0KqJJEu/LvE4D11LgtXLgbVwaI4Nw9aeFq8ui0hJySFX2XhTm+qu6JaTdVO"
    "MXq4Q+xGcTMsZI2syyTYGu+G8ZpWG2zuNxdSIkTLrXMI8uCSKRTEOXFQcbTWnXE7x1MNyatFttde"
    "W3hk5mQTGw2UacBNYKkOszfJfmXBUG9oom3gaJ5bQWo3bqpGGjVRr0nq031hcEr0ABC7fTg4tK5z"
    "DDt+H7aLE1NT4qyCsk2IFrNV4ZYJNL3ZUivb2rXbE67PeKxm8NttdZLv4GHDzEDRVhw5ZtZht0rg"
    "MdS1iTlbkN2XZtMk37uCdkW1RH3FFwE9umyEnJ0FlmGKqSnt6oIkyqtfCCZFSKabd5iOiGXviYsl"
    "XRSZSleMt5JXZBBKmpiiZ51ziefPJHqMt9e/gvZQn8dm11McBovYsJKOsustI5iESzLcOI0twIS2"
    "r1RKy55G64r1NyakgUNaDXODlGG3JhkjvBUmA7orMC5LjLs2h48C7khBFKkuyGJeucuwLZfi2+iv"
    "zM5Xi8umaJ6y7Ei2UlJdkdyDGBNSzTDrnMdby5XXBgfOFaLDN8uE1OOBeuJzQTsjDelZU2/ZUY49"
    "Jt4CidjzWxNypAtEVgUuMtwpCpoySYAE1EaVJffH/ickyYrrMEoSQbNbh1gW9ILHK1hoFcdVN0Km"
    "jpKXZHZVKl8YQdJybKI5lit8kk64dYPNQXXvhmbmWkmX3+ibLmoO9Yw+LS2F7FmUPvy7KS0zL5mA"
    "c0hhphvnOFSCb0bKtHhrTGdSpKv7oTj8lLvj2UX4w2csSlLvhiN1/ZDbKLaha13JCpoyUZoOSOOZ"
    "ksJ/KUiw6OpVFKF8YsbK9ohQ21+6sFPToYqX4bTWqq9cYbUrLA37KBDwDLhKzghe2rXNOmykCg84"
    "lpGBLyzUzMN9I67nyupIQZuUlnh3KMMTcmiiw/VLF9Qk2QngAyylTKBbdnycOueGzUYmAKal0Fkx"
    "RvkUwfu5Rc68c4Seo2NqfGEuRG2wybbHUKQXKFtsEq44WoUhpBmTfVNhMZL3xLPPzsviLdV1A6T4"
    "Re2hTzyarktBPrBOvlcZQsxNPJLyyLbdSqqvUkTL0rN0dAcnHmcm4UinGWrkFVEQXdrj/wANAnH/"
    "AM+6nN7EhVLNV9Fr+dm/2DwMn7JivfDzaeu6nfExT1aCnw4NLM7cITT3LGlXtoy9v6XDo1725UU+"
    "EaVd2qgAnx4WXPzsuBRJs/m5UE4H2l1Oy5jCuL/MtGfdFeDRjm0CNtfjE67+bli4dEv7VaUF/urG"
    "im94ma/HgllXVWndDTOwX/2RMnrudJe/gdT81NIvxSBprjKWH9AIPjzAix6y2jwGBFh4zRNiS7FW"
    "K6QRZWXZWrhll8ImGVmJNCeKreFVFUtld8E2eRCtF4JuVaXx5ELiD7dNkcbnSSWtqjYu5XF9IFGn"
    "pYphi5bGckUNuW/hluKoriy1yOAOvNdcOcacYbmJhE8U/qQetI45LuNu0QRfRteauz3eUovkw0fO"
    "Ni9KuObdYKu1IeZ/NmoxMT8w0j6tmINCuq6OMuL42taokPzZtIE2yQ3kOo0XgWQkm0EjaTHdXNVr"
    "shzkA606lHGz9aE4uioy42Lgp2w4s0Kmyy0TpIi66QFwi222lG2w1CkGy42L8s6vLbKHWWa4esa7"
    "omX5oMRuXbusrSqwhuoI2jaIimQpEwM02nGpdm4HRyVUTYvBLLLNJxyYavJ0s7UXdGKzRapQhJMl"
    "SJSZlW8IJgFVQrqVIZZdrhkvKpugZdhsWJVpeQA/tWCUEEwNLTAtRJDJyw2NvtI5bXVCDMVwgFTN"
    "E3JDYI2LLDXRtjsgNHzraHQCwHUyUeBqcRpHJp4yRtT1AibY40JePuurDOkGWUYM3CB0R1Vhlr2z"
    "QYc0fJNi1LNnyvaNU38HSF+lHKMl9/CJzRFMnMDyJc1qCD7SwJHIS1PuBRUi8TxW3vGAa7eBLMij"
    "ijopPPotTV7lIC7kg2XpZmVxksxmRoowbLnOBacDbcuSgZLzkWlIFpphuaw+TjPjVSgpNtoJR5eU"
    "OGnJdpsX0Sm3hAvZWsPKmSHQ/ikND+dmlL4JwaSY9uXu/RhoE9Y0SJqmwqfDg0U/7TFn6KxpZ3/h"
    "CH6S8Mk/sdlQ+MaSP84bbaft4MP882Yd0Wwy3+alwHglHPzL5N/HOHndjMu4Xdw6Ld9m9svjGkXt"
    "gyxJ8eCUL/iIkTIey4Sd8aKZ2o0R/pLwTjf5l4HPjlEqm4q/CHT9o1XwaClViUdFslApcU1alTZH"
    "ML4RJtChXt338nVVeBkrVIRNFWnbEzUCWpqqLTXWOjL4RcFVGwRqqa+TwKKIvjGzAVpqWkUVo6/h"
    "hg3EIRE0VVthwrVRCNVSvh58Ofkq8Lf8oSOM8AoF4u21RIZaYZwJZlOQF1eBHbUNKKJCu1IR2X0a"
    "qOjmNXlVEWDM+cS1XgCV0hK8ZBslVtUO2lYWWkJXizZFcaqdyrwtMaRk+MYNcMhctygJaSluLMId"
    "68u5VLgbfa5za1jEXRa31qvj1pDj7us11cDktOS/GJcyvpdaqLDzWjpPi5PJaZq5dlwrKz0txhpD"
    "vCh2qiw5LSErxYXKXqp3V4BMecK1SMab0cpPFzlF5URVi+xGgEUAATYPA8DzOOw8NDCtIMtHyCtP"
    "qNqGTt1PCdImiMyGgmJ2qMf03/Nf6R/TP8x/pCLSaL7pPoqL3QZgNgkVUHdCs4TqER1I23LVXuj+"
    "nf5r/SP6d/mf9IdAm5h1DClHHrk/ZwNNC2+2gDTxb1tV36o/pv8Amv8ASP6b/mf9IVjDdI7rhNxy"
    "6nd9np9hr9lavs6ur0in2fn6TX7Or4O/gziqf1Ey/qPT0avg09Cp5fL+omqKeg6QeeZB9WWxUUPt"
    "h5kpRuXMWlMDars3wDrz7MsLnMxS50BJrajh5iVeSqb4eOog2zzjLV2QDsw+xKi5zMUs1huTK1Dc"
    "5pV5KpvhZYh8chW0TbCSfJN5Urlqhw2JhiZw0qYtlmkMzKqljpKKJ2QzMkqWPKSIm3KGgKclgccE"
    "VECVa5+6JsrhXip2uIn7YcmKogCSD1kS7EhGimZYZlf5lTziZRuiHLpUhXXCMS9LtaqupIIF1itI"
    "laUcKYG4ESHzxG3MAkFxA2VhZm4RC/DSvrLGHMTUs0a81CPXDsuVAcbBTWu5IeNmiA2NVIoubnJZ"
    "FsvIVVainwh5Gn2XhZaxCIFXgIG1RFEFLldUOTJKLbQZcr1l3JADxyVR1zmt35wcsqi0baEpX6kp"
    "BzITLD4AqIVirDCmqLjN4g0g3b22WBWim4tErDTqPtPg4Siit1gpZxRUxVEqmqHGHFRSbyWn2HpJ"
    "TwyXDGgOaizg2mWmJYTyLBboqxKrLcSU220bMH0S5Kbok8dyXtZS25rIEyWHJdcOWeZNTBB5IOf6"
    "xLPSz7IkDQtm24dttI0YyLomEqFpObFXOMc89INDhNrvT2vdAk6YqDkojanzkFabYedRdFgltEVt"
    "EuOJMRbl3zxDqLg3UjRjLKMNvE45yA5KJDMwD8tgNYaVxx1DDozFFZmqoVFrUCiUk2+WMoeI594v"
    "+0LMSpaMwCK7EcREIe3rjSswrgo+ViiqZVW6JfifiuMOo5M1yt+72Q6qZ8tY0YhA04HF6GY88OxY"
    "KWN1HpWaHDuFdaLqWJaQbWoSvOX2jXXDrjU0wiZX4jlqhlBojyIyMorOKW3LXBMS6o1JtMOIFcrl"
    "prWJq8kH8lcTNYnbDBtwmKBeqJn74JNIuMOTeImFh2qqJtrSDM1GmCfO1QlLGHpX+aHkiQ9XXEuU"
    "mOj0k0tXEcpf1++NLTmI3YSG23QsyVYnQUkvVwKJWNH8SmJcQFgUPEMNfvifEsMpFCQgECtVXKer"
    "1RJ8WHDteLkKdy7IcRtiTdC4fGE3cvxiaIFQhUtadnlKL4dfSxcZKwx1LwDissTDgcwzHMYJxxbj"
    "JaqvhIgSzBuCtRcIc0jENalWsG+6iIR7vRKfYiIm2Fl3Uo4i0gpQQueHWibIxXEEmq0uArkhpgMl"
    "cKlYJZDRQuoP84/yiXrtg8ANWZLqQYEnhRQLmmK3IsNmmEKOJUbnUSClBC54daJGK4gk3WlwFciQ"
    "LgiIgXMvNBu7IJt4VAx1osccnhNxCOxpoVpcsMMSUssq4RWrU7o4krLq52cYxPW/DDrB85sqQzMm"
    "PiXeasOzQj4ltaEsMuOJyXhuCJYXQtKY5iLHFqeNvsp1w84SJay5hnnqWFNq1AQramVucIZKwgrq"
    "XFHP7QD8UOC5IC6d4+MxVjTDNAJx7mCZWoXVWJgV0c3Ktu8lfGrX3JWJd5zIBLOEmTKjSOXq/flb"
    "E/LyFEcKaxUCtLwh2VmenmXRwWa5p1xIsVzYbbA+2NMMKgE490aEVqF1ViYRdHNyoOpavjaqvYlY"
    "lXZWRbnQRoR6RUUF3UrFHgBs2wQKAd0Izo2nG2XF/Fau6NHvaX89CYr97D64Uxosorl+NdybYmHm"
    "+YRZRo6UmloxMNEir7BXZFGkZaWWstLi2IfeW7Mo0bOTFHDbao0zvKuteqNBOulcZFVV/vxZxAcT"
    "jFMTFLXXXE9LOr4maMm17a5LDGjhpVrlu02mv+kaK7Hf+r7T5S14LblpwVqtd/DmtY5KqnBVMozz"
    "ilVt3eFWBJ2Ux3hK4TVyndBuuLUjWq/8msvI5/a9f+SPX5DP+oNfL5Z/bXV/ztr/AF+z9B6TujpO"
    "6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6Tujp"
    "O6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6Tuj"
    "pO6OkjpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6Ok7o6TujpO6O"
    "k7o6TujpO6Ok7o6TujpO6Ok7o6Tu/wDpHqJmsdB+skdB+skJjtqFfQ7rUBNl8XUFz8C+i42CWHv9"
    "MY/FDwtvOCKLqQo84d/TiXNxVIr9a+/0I3XujZSsKqkohsFIRQJVHaKrksMzTOQvpVe30KjI5JrV"
    "dSRy/wAqmd26JtXaWoGQp7/TJf8AFD/b+7glvx/X0KbYrQnG8v498KBpQk1wiClVWJOVXMxSpehG"
    "xMCuEa6x2Ri6PPHb3VzidQkoqDqX3+mA4OsFrF70oqmuuPM1gGJdrCaFa+hCba0IdUJx9pQd9sYr"
    "JNK67sJYVx1akvodzBqKwdWExySlyf8AJ/8A/8QALhABAAICAQMDBAIDAQEBAAMAAQARITFBEFFh"
    "cYGRIKGx8MHRMEDx4VBwgJCg/9oACAEBAAE/If8A9cujLauDvBn0jVM4z2XH2jhCgy5iltlrDiJF"
    "ViYA2R2SwmBcq9gU8/uKPZY03pLZrz09UU6v0l2uYUUOaNu/EOcCPrrMZ6irKvu3xV/eDHcVUM7y"
    "o/b1JQ+5+46gxCWkEPAmc+1febwQG8avjoDZYm1dg/nqafKXiDwJsglPo/6LYeGmi2eI9/GfAvDa"
    "oYhHHsl4WsG12IBVM8rW7D59PWftH8ylcl0TG8vobZYPhGGXlJrY+EVXjOLpEmD8Gke0HWO4v7H4"
    "b70uZLpf+UVguW22ocb6eeIqFysSe/UFLDSlKbOgtlFvkP7mJSG0jdKfxGVrk3i8Y5PrKtU+txBl"
    "uPp79DdT2jeB2F+C1VcQ6B3M0vfvv/CLAzlAzYFxd0s4g9cLWgwo9zADflQY8O3eW2recvYotRmk"
    "3McwvPBrb0TiNk3wauiKmIrmVT7Hf16XsV7Y1cVmM/G0Afz8XF+olea5lSQ9jRW8QjS8O9v/AGXW"
    "7Z6HY6dpeXpvXbiV41PP8+jzsQc0kegc8Maxfl/0ScWTYCoTTlyeSUoAFyKLPFkx9iO6p38UpMgv"
    "DQFXhd+23Ey7+wVrbW4kfYJjhhrg1LZVyVsu2PBMOzD9EBaiqvFXCVlKgKBTK3s1iqgvLDfNjglh"
    "Vc+ZaTJWqprthogFAcRts0CjmOowZ4n2n8y6RRV2C4fjWmKGF+jP1Hbp+57+nC36j2f5La7TOkyh"
    "fyemiONvVXlcq92GyVjrQa8XA9LlAbS03XMt/s+P9FBI2kunx/8AEpdpmXBYxrEqbpfp9FSpUq5T"
    "sY1PE6NLC7Et9oYoK0j6a/8Ax4hoALX02OJ3k/waQPVgjkbP8V+ohBjJ/wDOLYdUwxdt3fnw0DWO"
    "0qiLs+TaXy1ZqWY9RZjlunufMwukcUNcKg5goBBq0oq36Rthr37cQNYXa9vWZkT02lWoc3wZiuYG"
    "Ali0OESLZXOCLpbF94FYypiifvf8zS255fvKuwqrsqvvPWa3iNsU4mh1rvyN01uca9cmnanvK0uR"
    "ms5rxHdplcCGg8yiFhZql2N2vp79gacRog2PdEt3WXm1poemZQO9Rb1Z84oH8TGeI5qyDpRPI/8A"
    "CIgFFjVWnsE+BmQw3L/p8BSW5rQbanNC8jBFmAHtNJj5tAUNZt1uUtGessYC1rtjPactOUdhdGf9"
    "U5LaqWhtb1LfCBAV1p0/x9QROaGrP+AXFNJXb93DFrLVq7RTZqjq2P3u+wdQwAmyrcFwS2WrUuCg"
    "vfHeJrIWy+NvNbj2ONM0vVZe3pP0HbNsrcw5KNl/Hm5e2zOzS/TXdy7QdZM59vYTvHNq1BZW3Zen"
    "tXSpmRMYB2M/SP4n2TOfiEHui3gbZX9sSgwkZ3F9HmCqo3VSHrOyH/hAba5AVpOLDj/0jpKMyIKu"
    "O3tA3wQ7XUqzT9pqWP0syov18SonJQiMLQX4xwXq5YIiVCGwxbY/pBKgFfwk59SCCi5y2L9qMsSA"
    "rS63y7rBcEENjlnGI+bigPyGetBsZSsRFxgrRer8RDUhQX3HndjtKRRvJvEuyr+5VUkGrl+7/wBN"
    "MHpC/wAyxXzDDGePznTBxoOtlm7A5z99HN/BRunaqUM5fqx6AeMvC84ijXx2gq/rFdpUtTwP2o6D"
    "VoC4U575rD5tj+PCfQeLdGR7sTu5m8fERv8A3g+0TGAUrmu1wGCx4ZupAnb4gDIQCJ8wchEDadma"
    "ASUj6DglSFjDZgKA4lNfpb7YlX7uD7ThGVXZZd37R2jJGy+2qu95ggCyBE+ZjyNWLT0iFO74+J4l"
    "534QCgUVg9oyrsyFD4S5+wfxGiLXgLjZrN3MLsyBp2qPVwpej0ngVBHwTwzdonLMaQ/0wuxvkAGm"
    "sl15vvENcRRaoHrmzHnMALGx+vL+aVsKvfE5rmxgz9dYCqlD06363jgqUTvJhJRAeZb/ANsfPiut"
    "ll2/CIXLlsC6mMWVbOlPhH3lOa9GzT1BkoJaY++8DvVD6PiHYl45pf8AEewTYL1ZvfPbjo5QFRe9"
    "9UkzBNtKR/H/AMJ274OT0dj5JgHJpWhL2TY8RqCcWbDhear7/V6kOzhq6zy/nicB4SuJevP+T9L2"
    "iPBWNtHxwo8EbX0NfEu/VL5yYZjL8uMQq3l6X0luMFxYxlnS60UeHrt1EufOppbyrgs81DLU+GKv"
    "yUrvaP4DB1VNc7XcsFB1sZaNYlKUXbC5mllDSFIQ1q48AYEaC3uKc0Eyxj2kT8gQBaeaL4eHzDHz"
    "cVk5YoFG1i1o3CO5roIAXqrxzb4iLU6TXgUmVOdw1UkANqxHGVqswU4XkVZRBRYHknMsbFqBBIcF"
    "lvK1GJskaFC053A8hG0rCi4rVf8Ag/3sxksC6e52mcugVF/6YF2CJEbkJvc/aAuUO8PJrjctnWVV"
    "ZAZrZRjXEz2AtbwrJtcd5QeBsLXks4eCpRKhLoF9bPuy4KHtqP4KDwEtamM1XBZpTu5MRPpu8a/g"
    "+IYvVcurbpdeyog693d7bbvaAc0wSwoujVHiE65unrh2zWeCVEk1bxTHYpcazL+MhcrH3bhneq8L"
    "tatnhxGrUtmHa2OxtdRLa2ateRV3obvUznia82H5adlykCVuSw+ywhiJq91jVpdLXOzEP2mB4/8A"
    "5K333333333333333333333333333333333333333333333333333333333333333333333333q2"
    "xKt/2Q04C7G//jHFXmFPEznRe138/UMrKSCgWvBP2psmy/469j+Zs1VDi1twf66GuYUbP3yPgna5"
    "cvNvmu5Xjd6wy5KcCznq0Lvx4RNtpA3U/AMHXUs0pVuYLL3mvXuIofeLEggI7tfjObO5AwMdjsBb"
    "LcZeexELpYwEKBVq198QCTLiLp5Y3509mKxGSU2t/gbJdNTdGIbvN34iZsHzgu88r5jRNxMStGSj"
    "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"
    "Zy+E874f1PI+E8j4TyPhPO+E/cP6nnfD+p5nwnnfCU8vhPN+E874TyPhPI+E8j4TyPh//keMCpgC"
    "DFi/XzPK/fzPA4tiPx/piSvlKrPQzE9KZSxPZM+0qv8AUuWALacd63/uCxcn9YbVFIAwT/rI+N55"
    "HH+lqTbce+/4ZbIedgP5n/inCEuBwoGMP5v5H/SU3Nir3JRt0awp59a97lADCmhPd/ufseJ9k/Dp"
    "95/P+lfPTfkYcU+g8MRoqgOWZYKCu6ar8r8f6WRgVyKQHTrH5mA6+k9/4x7xvxIhXHD/AHCPEIO8"
    "SZvvDmv3if8APP7mgc1c3nXYz/pWZ9a7RwjVPJrt+G/WWfiQh869iWDHl7eP9Pu6KtPqQBAs0wnf"
    "vzr7/wD4/wD/2gAMAwEAAgADAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABBBBAAAAA"
    "ADAAAAAAAADABCAAAABABCCAAAAAEEAIOIAAAAAAAAAAAAEIAFOEGHBFIMIAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAABBADDDDNABBCGKBDAAAAAAAAAACAAAAAAFNAAEAFAAJBOHKPAGE"
    "JAAAAAAAAAGAAAEAAAAMABCCLKCADGCCAAAAAAAAAAAAAAEGAAEIAAAAAMAMMEIEMAIEIAAAAAAA"
    "AAAAAAAAAAAAAMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAABAAAAAAAAAAAECBBABCBBAADAAACDAAACAAINAAAAAAAAAAAKMMAKIAEMEEMDEIEE"
    "AAFKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAD"
    "ADACDBBACDABCBADDDDBCDBDCAAAAAAAAPBKAAINKBCJMKNCKOIAMJEGJLBLEAAAAFKKEELBKGHG"
    "CDGAPDLBNCOMEEMOMBMAMAAAADDINFOFMAKAOIBIGGBMIPFLEPKDDFHIGBDBEIAFMFAJAEEIBAEK"
    "NEBIBBDPKJAEHFBANMNKEJEAAKKMFJFAAFEKIAICBOCHMNCAEGAIDIOIHACBCAONPNJJCMDFNFHB"
    "JMAEAPCDBBHFKEGIGLPABJICIHEPDDMFOCKMFFHCMKNBEADPGBLPMCMFCJPDCEBHJPNGNADADNMJ"
    "AMMNLFONPACLGHOKKFDEOPACBNJMIGIENFFLACFNJAIANKKEKIFLNAKBEMKKFBKFKJDEKIMAEHKD"
    "MDENOEIMHOPNHIDOAKCMCOLCEIFNIMFLNEIDGDMFDHCIFIOAEKIHFKBLNEKPOIGLNADLCCNBFIPI"
    "GIBABPCMGEPIJNNMCAMJMBNNOGJGOHLPGMFHHBKFEJMBJIBPOPGPAPMLHBCBBOOPLMNIKCBFOFLG"
    "IEJJEPHFGCIIACMDKMAPFDNGPNAHANCIJCJBIKAJACOJJMOOBIEEBDOANKIINPOMDCBDLFCEPPKM"
    "GBPALBMOHDCJDNIKJAAHDOJCACCBDJBMMCIDBCGFDKNBHKOGBKPIIHKDHGENKBFLGHGCHIPIKDGB"
    "EPAHFNPOIJMGFDMOIABBFAAEFIHDNBMPDPPJDDCGBGLGKECDLMEKDCPHIKLLAEOPLAAJKMNLLIAH"
    "NJLKHOLAEOFHMIAEMMLLEKLAOEPDLFEPGCJPJKIBJGJKCBLADLLDEHNCLIGMMPOLKMPEJNDFHEOD"
    "AFFCKKCLJPJPFHCANGPMIAAIAAGJLCGKGMJJDDNDGNOPHDMOFLDMNIIBAOJAAKGMKALJJFNPJHEF"
    "MAJIDKDLGCKIMJNDCFBDNMEAKPFADPAAJPDDJEEBJHMLGCBNFJDHMOMNKEHACCFCBHOJGFHFILFL"
    "ILHCPIANGHAILCKKGBDFMIAEHMHBIDLPFLIFAMEKEPHLCJOMLOPBMIIBIHAANKFIMAFONPHDFIPA"
    "JFJJAIOBNAMCHOBPIKICAPIGMKCLBLIOOGEBHKKHKEJNBCBNMCGCFPNJNHPAMHOLOHPBOJJNJDFA"
    "CHMHJIFIIBILHBMOIABHMDNPPIEOMIJJCPIHFJNLDGCCMLOMIFOMAMCHJFEIEHIMJBDHDOHFNNPO"
    "FJAKOIOFANIFOAKPAJJOLPEEFPDBFPJMMGCPKDPIPKAACACMCOABBLPEMJENILLGIEMICHPDPIMC"
    "GNOOIAHEKAHIEPGKHAKMHBKLJMMHNCKHHAMBJDFAMLAEGNGMCEGEAOHPAFJDMBKCBOLDHHMGEEAE"
    "OBMAEALACBEJEALDLBABOHAJEMGOJFAFMCAHGEAJHGEDJPJMKFKMFHDDIJEDJFMHPHKKOMIKJFCO"
    "NODHAAADPDOCOJAELAEGDPLMGDKENEOLCPIMBJFLKCCAKAMECCHABNLKODDJAOAGDHNFKIIJFAMJ"
    "OLGFOCCCGBLHPKIIPHKHPDCFFGAACHACOLAGPHACEIFEIOOPOCFIKMLFDDJFJFCBFDCNLEDBGDHL"
    "JLDBICPGENDEHFKMODGIPMJNFOHKHCIEOIEBNIAMNMOAMCNMJADGKKMJBEMMADDEKIAIIICPMCFO"
    "GGAAFBOGLOEHMDFBMBJFOCJDADAEBAMKEOAFNADAAAHNMFMGGIPCECIIPFNPJKLGAANBJPDNIGKD"
    "KLAKJILBOIIHNJACLKNOLCPKCAAAAJJEMOGCAAFKKAJNGCACKNPEMABPBHOMALNCCAFICCLLJCGB"
    "HEMCDGKIJKLBDDPAADAAJKLHPDDHCAMBNFLDBFDCFDFHHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFEKAA"
    "AAAAAACDAAAAAAAABGAAAAAAAAAAAAABCAAAAAAAAAOFAAAAAAAAEGAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAALD/2gAIAQMBAT8QNA//xAAbEQAB"
    "BAMAAAAAAAAAAAAAAAABEXCAkDFAUP/aAAgBAgEBPxCrI19Cei7S8RHgLKYnd//EAC4QAAICAgIB"
    "AwQCAwEBAQADAAABESEQMUFRYSBxgZGhwfCx0TBA4fFQcGCQoP/aAAgBAQABPxD/APXHObUOCZyT"
    "HxUI9yCIoIFXSL4IfwOxSrZZUUEilk8NmNDUKtd/EDIdkRUytA0oafuPOqMrR7BTDvDdw25Xsbb9"
    "iR5VUSp0+6EqhXxUShJ0p8jjqVYYxNwQfR5alMleJZrE1sCiSGeFLHFg5q/6ItjhPtsF6guQKHDd"
    "bXKxoJ3PyvNYSJWuHyS5voepoTX+jELVu4YJ5SWqfYJ+SykjlSY1Roe88cBP15v+jWNEzHTRq/ZP"
    "B4ZHeXA9pdwmIRrtftL/ANR2Pf6/u4yznedBZZ+5dqVMDARitnhi7W29B6BEn5N8HBkpYDQSg5pp"
    "u4elGJ1usl7gcZIeTyzacIjT7esLI50ji6rwEU5WCdtsKF21xYum5KOTGgtogEr13qqN+y9CpKg8"
    "qyHVKyf8BQvmeCaliihZJfEIeGv8Lk1s0JL2G5SjJVEzlC+oilG40/rouL5Y1PiHbV5s6bESNUo+"
    "PWk6IVCWFPavzXAHRjQ80n9AlP10K1eHDSopUT0wkOrVdnDYo/UeVmlB++wMe7VUEfUaes7KJbux"
    "DfqwIaqij5Cuf6Cy/pdYWF6nuRHuXg9UxqIWLJHLbS97xNwyPbU45XgqraphcFJ7k8/6MtrS+HSh"
    "hl2gemve4r8mwnY9Po1cyvN4CZKSu1aPICG9fYC7EmHt+2puhzT3cM5qDNTT9LlE9uLjbIroMVmG"
    "6k5iXfBIRDDjf0KbSOhMusJjKo2Xafuju+vO9Qjj3sTVcmhItJIQkq9Vh2aJKr8H6/oJBUukttNH"
    "mjxn5vkptekDu36syjTbRL5kNtEJgI9iZNktqOxX5Ufte3BtKSZFUkZ7VEmX/ToUI1TyD3BJcxA5"
    "3/oogHltqnpiLnn/AOE3oq15ixqDC0PQUT8nb0NHshdKyE6GiQ7QlxHsGiqKJnXOPm8wTnLZBrJs"
    "J9KRTCif/wAe1zM9n7elmrd4iQ94/wAELc+AXwu5Of8AElJbXaUWG6Zyn/8AOZWnNMpSfsUKBjje"
    "E2esX9RIxO5mwOQv4BcQMBLZHTE9RKDVtPh9Rdba3K45EjfMR3qDCQ38zk7UDSXtvkoEqAnoQqOq"
    "pwsS7USSdqQrqutN0BWh4fNYpHA84dMFW23wsLPtoVvkpMfDVDZBY5ojdJM4O6I8JHAn9lUtxeuC"
    "eoXv0GpF9cDoyuzTrt9hRqFr2+W3PBSxvtUFwy0oL5CK+bTFxEjSi0itRriJRUU/+20JS0cPxOJM"
    "u3Q49tnxSg61nG7Ip0LbuFJeyRMSB5qXFpD4HJ/4S5Dj6DIy/wCKa7rT9l2hCAQoQKARvm3W4Vvb"
    "wVUGU2OqWLorke14ev0DMQpXNWG25CJqnKvf+q7HUuREhoS0vL1pwpNPaAlrE4HD8vUgIlUVtpXz"
    "/g8GUckkXfP4tC+BFFoqV3/FzEHQe0/KR2qm93VkutlOOF+BL5EJbT4bW/D9QtNHqU/4xfyb+vwM"
    "P59b0wuOXeDKNK58k3K04EKpnCUW9ezq/cSoMKzFTe0lUTDEbgoIKbfc/gMCW4KKS1CTppqownY+"
    "duYpRtVTaNFNmVZZ8IYJ3xBVDDuTRsQDgT6C0HCtVEyr2GmgZVygUw/6RctE8VA6thLmdG0ps277"
    "rgzTVitZ1/aUZTe1HFmzjaUOUkAQ3Kd18I/QsQcU1bst/wBVLyED1uqhOvicb/I3Vkmip5E96YK0"
    "MyyOW95fTf5qKSQNRwBkfXqs/iEnWyxAoD8vQ3Rp+wAp1XOzYAgD/run0jlNcNf6e0FTXtlZAlq2"
    "XspSuu+AF9J65zRFW9y0wJFNWoLYHadde/qRdF1j9IdDRobV6tvWYSaqJFcp+fdweBT05F7VAykx"
    "5F4/9eiJ+/0KiRBtCQPjtqEtXSa0a1auENmzbWL7TaBMNeGiU0KjPD397QapSeR2oClMjiahM5S6"
    "F5IhYv2S9Eedy1+mnsRJYmhItJITxQ31qeMbsiZDFRnKlq4QkZC9LxOfC9acjYxeVQ7cA9jTHOVl"
    "0dpIJ0GK4tLiRpelB8atQjpmOMtWUEemSF/CUjxgzWjUR2GFy+6LFBOLYDQ3Vox1wwQ583bdOEFj"
    "Em8ewXl024M7t0i1ZbMdwv8ATdzFXpHhebsBKI2Db6c9B9AU1lpTTlNet40vBqeSZJST7fIhCrNu"
    "Csp5vnn12phTy3LwS+W0BenLPeuxExYlabXpEkVzVZFRZZruEjjIXygzP5EdIEI8NXxkhbRPScDM"
    "DvXeUQ15bbhJCzNQTfh0V4nXsOzssQ1bGfIWq9MUvmZ51pDYs23O5dP5cvn1e6Fqdv8A+F85K49/"
    "WQmOyRXsFNYcTl7lOcfnN5iFzvn6q/uvj2c9mw+w/wB8a5ar/J+u7jLjO9mlJ0QgoEnhoSiYGXHU"
    "k1cpKTKG6GXFvWk5rNVcxoewMUcpWG2OhJ7f/tuVkSoaER4PyiqXEGvbiNF591olqnPhvApWpq2r"
    "vuBadDUSMaCUjyCeM09HP24d8A1zF4p9VZ+GyJiSU+eK1N8IXL03bDKqWegTxRJtLQtz8ETrEDwD"
    "Ls8UVNDLI6Ks3TeHXQ83cgSn+jL2wk467KJsMAKhgpp1FVOD8dD3nl217htR7jH823Nvn/eIqj84"
    "o23sOU67ONt9vy/9NP8A71k9jrWqZpl/cNgSCkcQppOipMKJEDlT2QKyYyC0D+j/ACzGjpDQRAFC"
    "W6bk7whi3iaq13Pt12Ck/BFVRC3CQmN1tIlJFuE0h6DpErMhNzHe+hMdy853fa8Q7OB+EiUJLJjx"
    "wRDNdPg/yk1wbVW5b1+gTbNouru30v7A+eD4Wj25m3CBqtXI7ZX99nVkgHrFsncoNKkXOtqz6bKL"
    "2Up0S9BqanaNOHHkBDJh96cOtWXwTMB7rwbgdI8B0hA1efCKEv8A+ZeIHxA+IHxA+IHxA+IHxA+I"
    "HxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IH"
    "xA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHxA+IHx"
    "A+IHxP8A9cLhJW0J30xNArf+zOk4MUe1/wDGTJoWVQkFzgSK5aRqk8qvVA0+hXJymiCLS4nnAXi/"
    "iD/S2JzINjrCklb4/wBdmtXUz2nPEqocg9TpdEnTdaOvbhOXYyrclp6TlogDhMsA1B5reSfjmSON"
    "W0ycTLjnhW0VBpalz73VwlmeKsSlMaa+iJGx7AVm7x7QrY8mLDbTPd2w4pQpBRtSsJqzyDd050Jd"
    "gw+T439jktSxQI3Tcn83UI5HJXYQv6Ec8BmYFk79VUJNsarRe0K9Nn0rXKn22uHcG1ZoB8zYki+B"
    "kNXq4TfrHahgHFXE/wDRSXhZybRDMJgpvwbmA/jMl8+TKOacl5cvcmt65SjgRfqGj/X3SVEX5Mp/"
    "N4PI3Q4Cc+NthQ4TvM8rvZLXZHVIVQla00y9l3QkLV1N59uWfGHK+ZsXU7aKWjRKL6J6xI34drhy"
    "h47zxu5G0+5F4c4VjpyRJsTlnjOxnuVW5rtDc3nF6Itt/CrlD0qTFLRPgbcUCGzhZQqsHwegs/R+"
    "2o9HprpsZQFK65VPM5mU7fqUltlJLkpNBhF/K2/+tiXVstL/AAppQpzcxdQ2XDT/ANdl0ulY703P"
    "IWIkYmydSIfywqSyh8Ik3H82X53x2eCUI5rH0m1lXwyqfZU0RoNhttQuD2FuQvr31HLacpA6tUka"
    "1Jl9M+gI8mOfsdYp5CUDcp041Dlaq+nse8+XGocovItDfRs0FODJRW2ctqBoaA7mLZHwDBXu1tlW"
    "d21KI5Tg2tISj2CSJZ2rcllPES0LhfNF9lebtU2grAxhN2GqKWmrQipN1LkZM2cf3jdueZRofCKm"
    "hJxguvtM1rE9CPI3Pc01ahbFYJGBrm0nLt+BXEibpVw0Z5dFjaJlS4SVOSYEJOeRRdm5S19LNrS5"
    "cjZXO5smyVyyuwE7/OUCk/a3kCEHIfCZcVvdfELFlykUJO9BT4GmiFGhPOhoXClxI/l9L6gVvacp"
    "Auxy92lVFdzYZAvD/nQirg1632A8AmzcZnp1s2gL2XgUOmRnVRx7ifDCmBzRmldPY9H/AB+e7LrJ"
    "rpsfZjhVp1PfMueXrd2g2jWRK4SJCDg3tbaQtMe5/KC7bbe3/r1LFqln4g22mzfrX6OLpnmma/if"
    "53VkscchejfA79GvMqW+owe6WfSS0pKNL0J+0YXL0TVfIhJVVYmJdSw8ec5aV7IS8H+CDEYq8Gmn"
    "tNmvkbicKabzbs3Zu2//AKjhMFqGPezgglcmJmcEEWUxyhKSn9Czz6gdzK8qb2wRPMq9CCqCQhOc"
    "dMJxp2ImvOUEOV4EIjmUi0vQVWQUTvC8PIEEVzMRPMKwhNmBEKcM4AichWJxwJ7XowQQSk9VRp8f"
    "4b1tXJfL/wADmDnjmDiGIFMYcmEImWVwYk1DsP79hMtAOtMlwt6ggfAnIJGYBUgtEaAYUXg42WwR"
    "zggSBdQDoJZ24D2QePCw5SA9AOg0QNtYOD7pOidtOOkbsfYv8MxyGHpeA/LHyTDYeCYIj31BRg+K"
    "FxbBecimMEiIJUsgukL+oUoYoD6JVwE8wLGALVCyGCEkIByl9CAGbUoIBMGiRItA0EH2KxBGX2v/"
    "AA32EuFsOg49BLYvMGmGGPbBgHiez4PArk0OZMe4+Ga7DhwARoPJRJFRhSSN5JaGTSTIjMBxeBqa"
    "4U14cjXgTA6HmQ8rC2wA4F6D7T/juFR4D5Bk3EjzwZgrzitg3OrCyQEYcZiqbOKCpcFA4wqbJhDu"
    "EpIwBYh5EPYL0DCBCG0DR5MGl+KeZg/Q288Wwh8wdmECjI5hCRfo/wDFCQlIHtQpg1THA/CAG0N2"
    "6Y8VasJLAjQGoRfpIXKwKWxOJYlXoyVgkpg4wDIVdjYR3YnlgkSJhKvlgnxg+BwzhA/gOQNiw/cl"
    "2ILKjL7D/gl6tIRwSZg54C2I4LZgV5MI0QBDsGgIzAmcmBUyCkwKPIPBUxOkB0gsh4EcET1BEgpv"
    "GNSQbVgu9CzBFOBVwouIRifzng+2Ym4Vkm7wCifZgF4JpT1RbMqW6XP+CWwgRAp6YblLCwIJpnYJ"
    "kDJ1gJDwqsNXp7mRYGHQeBCGnooHpA4JuJAYKsCYhGWwcgbyKhtiHWEKEFQQYZ9kKI+lHkvhSZIA"
    "UbdN+HMu6romiXtrBAXs6xykPt4aOCX6IHP6qvhAKZVvwHIsp4m3+AKAUR2BzBzNIc0CH0xE8nnw"
    "CgP0AI/SOHDOcnjYEEE9ZOAXEsF/guFjPIwg7bTh729DqEmFgQmNDgHTH7IT+JE2uT7F9qCmGoO6"
    "4OHPmq4CQsh7EhpLP4F8E4MQR3BFv+0bHfLrSgq8CIDJCVQ6BdjNmKCUpb/gkvB288fpCh4gkyHA"
    "WyE7sWHJMHAN3DEcGDuGxlNzWcDwIJMARej0HgiJWCQpiCtgosJOXAFBvJYP1AggsFwHaw8T4AJV"
    "XLGCtNPsbLKqLVPZs/YcC3m4SdU3e+wjhpNA/N9jdeAUbCssOV/cnQgu9ja9Ps0+zssbDYwvQlyD"
    "zNMVgcwfMHaAVSJTgVZsDgURmGEnQ/cjk1lCyJBQWEOg8hkgYToBxwSX98SY+gH68TAvbg2nJWPt"
    "mLuutYAqBx6e5uoZ/IY5fldV9o3xP2cmXf8A6b9BQrkXRalX6HwScGVcLevxAznJlnJRvAJXgw9E"
    "8OYOYRJh9Y0EvgQQwMcI88C7gD0sBDjZwMLg0QnZEQWCXBBdmGTdsDBTKwbxIjAXViWC9MpPSIcW"
    "Fj9oxQ8mz/0W3h4DgYUXlJaVwvwDTOn3nGNP+iOxBjoTGHQ9x2OWIYb8fwba/XBKp+ewRu/yeAy0"
    "GKIPBhquAXMB5YOgg0JuhJY1GSVh0BOkwcjEw5xxsRyqx3knwE1RnZMbWKzwF7z5DBQIFCEE02CF"
    "gem0HfIKVgey4NjXCNPRiRGVmGbG2CCD7Riq/VzCe3pJ4Qbg6XJtaldeRLIuByPCAvcVDcKbqYdB"
    "klngukvkaucZpVEnhv8AiGqkTNJHwtCb3EZNpwRJISpiErkOTgmSXp6k0MN5hESLSQER3sDLpjIt"
    "naBYLtmAXjGuLPPI6Q46F4heCDWnsKBIg6YbYhG2H6QYSPQxYfHmJIFwLzcW1JeTfRIlvm0tPAPw"
    "4TNQhBFHBY4QkAtGyEycVMDuyomJaWmr7nlDxV5wMHyHJ0x5KIfIJcCcBx0wdPAyIESs6n8th7nA"
    "hGYrALpBR0LGJY5EzyNO2PyYkDYUgmDj8ioOEmITY0CGhIiwYIeGgRLAw8PIUBhEDEOLH+AYkSNk"
    "k+gqjgwGVeBykGET0FDhSCEKl0LAxmgKZYNQlEBr/sxuEIJoNYEybfu9DGjfouxiuNFFMJhwA1rP"
    "k5L2DclkEkCewRpleArFT+poMIdMNiycJGLATI68vIP/AEX9jFfdf2cMPf8A2Ju/r/7P/SivldJ/"
    "7GsJxef9n/tf7GenlxibhJ8gVyNh5CjNh4ILpgDkxHgoUwWvoE6B7xREIYn2LAeC4PJAgQdVDl0H"
    "0hQ8FGEckNa0GJA5EK2ASZzdsrBwLAwghjAxxmhDIxGwEJ5BnFXITWFgTmBMxIRLnYJoI2DEw2K0"
    "Ph6So3EFwImGt6x43YDNEEpgChwcjUS+znCA5g8OMLwi5tCb3OBGKxmCDCQBsHsK3qKwhcATF5MP"
    "O2A3EQSS9ANPnB5KDQToIBWDaiwhAoHlQ0AHMAsjioeREFjlVHpHkQY5cKRBUbB6oe5jiyjCRDSA"
    "pjdec/JmEHuFtADM1W5Tqrw0BGyCYIU3cDysR6e/L8AbDl0ujCrDYWwgRjLLHR8JScic6/qutAHs"
    "UEs5bV6oHmj0JSMeBwlrGjUH8C6IxIzZ7fLY1NCIAUOPYFL8g9nAvXDuGMBGr/8AWekHkVZibBGA"
    "LXSboJRzVafAAc8v2kJxmvjeBCtv+wHk3qCgIEAnFR+hwMbamCokBrBlaGADN1DQDwsWpirEKWJ/"
    "HjDgSsIaKobAOBhZIsCJTW+AvsxJUg4wcM4hQW14H6UDxacaUzg1A73RwuljwP0wWBQnq1vKhAkn"
    "q+N9JfhUnhsUYUuffjy4BMPvYVxtvRmbwEF3AlosVxu0ZcrQ5eyUWyh02z+s/wDmMXnKT8ookP1X"
    "c5P/ANZs423R+b44AshKAKMFXvYjesC0RBkQORNoBE+x23BQBuwMqwXUQKbwLEKFgf1+QyDvSQ7Y"
    "Khdg0yS3QOsioTr3Z4cEFMTgPEaIXADs4GRAlDMOlcCc+i5GtyIYf3vgLB8Hw4vMB4kD78ljiKWM"
    "kF5xma2Vt/DR48t7ulgeCxivK7QDflZ/bfSBcheM9wV4CkxMgnjgIJ4BiIrgOSnfR/Y0d8KRF3Wl"
    "Vi+n2tgxftHMmCdH10MYP13c5oM3hGzIWgP1JIYUjuoaFyRJQj2Grdy5AHJjAoE4cKYnTgwqi+kE"
    "SB+eDDXwLguCkDkQWDEIdvQ8CVQVk8AAMXDkIwXxQsQfLAhsCobw/QOuGxwgGim/i7FApCU9qpnM"
    "EHKIaAopIX7jeRGWJ8SoAbTq9RpLbS/cGDpZDgAvbVHuxgAPbBapwAU4SVqUTaLkBE9FDzcCMIPK"
    "1GyNJ6Faw3T5mUNgM0UFm0uL6glYIk9dpTjZgKIEq4xYC7ql03SABZa105NgADjNlHnOtNSAeFXR"
    "cugAkK+iB1bAGqhAjjhH4i3TItdSYOJEgDO4ZeECoW8EO2FIQ4OrQTBqgj+kyKCBNDRiSwPsgFbH"
    "gRi8QiF4LAsE+xEsYChh0QMROJskpHkFyIDQ4CRgX2+xuwDYXr2AmHqSDUEPkH9cVBMRuD3wQG29"
    "WsbNQOwsMDhwSKoLdn0wJEl5wGD/ALmZJBWGpBOEIQMLwWBDGEKwhXqByY4wgqYeFAst4hzthluI"
    "Xm84khKdhw2RwOQ40mG/kP7AasQ9BoYkLoMs5xIvIBzggYQ3gB2nElQd3g9EToLAdEnJ9ZXbEyGC"
    "3DKv0IhhBWHOHIhQEtFleJID3uDukEzng0AIlp2PGobYMujJ1S8h85bD1gPmKEhAeXJFQDn/AEUr"
    "OFWgEewN7+gyPQI+HAzyaFWDBCAwclicKbbH5hHHnlF6A/pzWDzRK/H/ABKapmostvUQBUO5ljUl"
    "iwN713J6kFWxYKY6CHClxVnl5IA0ERNQu7wAJO9hHwGBxiFWibhWtodyIAJwtBRtuFABU13qgkwh"
    "+aRsnFAm2Zg6B5c5N6Vi0ihCJsfnc6shHZAHcfS3RAdJsTUie4AGOQZJvZu6m3v5yQEDOvYBDWAC"
    "fYVXl9ARK+5UbJNVYck6WUDIrE4uJIAZs4sGl/MSWicAVAgIFDP5BWE21ds1/NNd7xAQfz4iFYwB"
    "EyNDb+Ah90CPPNJI2mAIcHoaR7/igV2LNhPiCW4Eeboe8mLEwKZL1YQpIDxBiEUQDRvQa4NThsi2"
    "VShB4whr5IQsCvQDWmD6Zl8OYVCGEG5NUIBQwHzLGAO9vfRsAglSpG7LxDpmgX1K3IACopQVBh9B"
    "wtwG0At34skNTPyTowAG0V9wuUmAG3kDZQKiKWCBBMDHxV4fGlAYnVYSLJNP8oEw2tc6ngAC0auR"
    "wACuns1Ql0ohRVK0Ra8DFqLk4AA+sEbTQgAV2trjr0QA40BrOfIgHW+s6uAks3QWWHQAQbmEnfno"
    "ItCxmynbaQWcSGPEBj8yWFBICrIko3gN/HHx8gF3DyKiJy/6cFwKAoZCGl4JpALXcwt+7aDWTvJ2"
    "vBKDaTcG1wCAFEA2Ga0COIJ4UJLA2B7BODEDca5RhYNB4endA4YJ8G7KwoIHg2gwIA2cMSeGU9IZ"
    "VACoRhzYxE4Od+mwChjg2GJoIVxCgUZl9zQKO2hvA2EdhE/QG4PvfECI1fLFzIcpYNcP4BAHHS+w"
    "8wJwPDkXuoC5m6Y9CyFksQgMIjYJnkqeBg8Dw5FJFhBCjYkwAkjySa/0DLewE704AW8F9H182k4s"
    "AVf/AFt5yAPlZri+mQrEdEiQTBzP1S7G2AE630Wt5NgAZKQs3fnMKfsA+KtHR1sA3ZMfwkAlU6w9"
    "ISYoxhjNgQM54nt9+pALSEzhpMQ6SGAC0AlIafvQH8Yo1J9QHnmoWcBrgDRvvFBBv8KGrd0CAj8K"
    "LNBAgfx2xe4YxvWFEViAwT6fpUdoAPMQXsiP8Y926YmPMVDp4gc+kCvwldA76DwA/cP0DWuM1vCc"
    "8qMQ9ywJwUeC5Yj4gCGosH6KPANPGMOwwdseSQmmXBKFOZLN3AKS8Ut5aTABpxr85lwKZLwOi/sh"
    "3NuCd9RoEKvfOdlCwAstubhL4BAOTL76SAEJjDoWMKC4+hGCQ640XugVcxbuTjbbMDEqxDzT++wy"
    "GgDmzhAZRIEaiVINSwr9JSjgr/S3vAC6+MH7mmBd98KVatYAPtR2gPEgOMca+yaA8IA6VAIAWQ81"
    "SZgAeoI73Qh0x6EWx4GF6SXETxBMcCgaFRQs+jCuYDsaDwQ6E4BWPNpBYFB6g/QkdgsHbU9BaFmK"
    "YXdiCgweIsHAbVkCAxYFnuTIpB8omMiEBeEE6+vI/SwWSy8JgCuVPBh57ZkaggnAFv59Fg8GFSPD"
    "GtyCrhSSP0AY0EBBRExNjJsB+CQpwMHIGCB0HCGg1PRq7hRAlfpArBiReyAulDO9hKAt31yKeWF7"
    "UmYaQqDoHx/BcDN3JIJ6gOOYClBYREyB0EMKygLBBhfcaC4BBCwSixRR2lkp2mONg+WJJ2IYgKEi"
    "Ma54gO7Afoqp5FgWvBhDsYh/dFWy81LSbgNqiyQQQK2JEcKTRF9oDlCOvmEAESCdyGtGjYRtMKYJ"
    "2QgLxJyJbjTDbx6AhiG4iP4UBFpDhuxMSgQQgoMBXyQiIT7EvYuFdAJ6bCwNtvQb2Ei6AkAuEVTg"
    "p7IxoKzByB8hPplQwsFgomILeIcLGhYBwgsk2tBEMRyAskOQtPtBthpheSwLIgZjJRFk0yGhQiHg"
    "CMwj7+xYCBqIAoRcgXgLlE6OGEW/AAmb+izyHzcBaQFOjf8AKgq78EAZM/k0gcvjFRnCxvYCAxwH"
    "OIvs8Kk0TgH4ehQ2+D8AWy4hkATAWJnWMbZNAYUQ52JRYxcgTN7P4hD6fJeRgzuqNxrGgd91ddzA"
    "LGxwoaBAXF7wqci/ImBYbwCtwONi3OCDoEiTAu2nCIVIOIjILHIaRjuChQMXhjWGYXAumLY7CAwe"
    "XxUj2WDo3sIKV42Pv38y7O8DU+bOBwW1XKP8UgNR8AYBggCIERI370CNgpTyxMJAbBHxN76ufTDi"
    "gs+hSlyzXNHuBH4GOgeFHAXTTVBt/fJycuH7v9+UAE8koc1mh+QUGhECwyVIEr1+TEV0nhPJqaQO"
    "88AO5tnkztwExg7UALvkqdsIHwHqxIKpKTIjvjsC2D3v8QAbsAK6YJQzCdBBZyVbW/gJPqWOsSNm"
    "22HsOkCZKyn9b9AH8SFGB0PSLMWPEBFv4fpD+GA1kgbugEsAXLIxg7nyKBOwglmUIWDxBoWFO8we"
    "pE2CghGCV6cBPWHHVYE3F3Wc8ZgAsA7yTcIALBO2mQAvnPlFCWT0OoJB/wCLcaUgGwp1M7zh5EB1"
    "eCkKgA5TYFkcGX3uLww8g/h5xgB9njZxAyDSx9Z0jrfMAA1JVZ5YKtNYB5PRsDeSU7DABzanOYAI"
    "J5wKB4julA0ZqENBQAVxihax0QCC8KAPTPI6FE9UQqMAmn+PAG/KeSn7wBMwW/sbEcC8C+4FrMsH"
    "ANbAymEyaxyOw1cY6gL3U86gRyMH6EoqMaLFJIuEeRCqRBYYg/E/Zsl+EOTnDdYJRMOBOLhGVXON"
    "O0gVvdOCR4bBiY1QB2wLILANCU3Yn0fnQtReWCGL/kxIsB14SkLKhd/ZzgqsiBwjAHWbA18Qg6eg"
    "WwhiIHEi5iyRslvBWKUoEGGCw8gS28sG6KnIMOMK/A5iyfyzc+gOAi93YEAVqdHZeyJGKplMEhDm"
    "rOgmcPwGRlO9WaFhgCmtVBwMBWFBuqYPLmp6WSSXoB8pSFE9YPAcAsLTJNvEh8YY6NR8xJw+X85L"
    "g++HqkW8BtXA2eGK4ZmXBMCZ9hXPeXJcZWCByWH3BnIFjTSc20IWUuw9ZAnyGhsI++weCTAelElg"
    "hKcLbIL0FgMBPKAUItALtgXMEgOZh+JEvByf6cSaJLxjPZ4dDVay9xHBqAHKlIp+Lh8m+wBM3LZc"
    "QofP/An4tZbgFN5oEQHH+sGcTp+1/Qh2Vku/ww+p9HIF28QGyE14TagBR+0+y8ud9P8Apz12ieNw"
    "AWupCRqPAheUIUCcgqvUdEmUAApN94AlqADXxRgjnmYC7jqA3WBC9hFMEu5hmeCMGCQjdEC2sBvg"
    "qwSfoD4h9woQoNByWAhDkLjYUMWLiTiTCBYIO2CzTViXBYdMBEMAL5IaHrwDFAHxETzrpN9XYHOM"
    "1A2wPGY+kADxzXNEBaKCuwOBXW2L/A4E0+5Rh1GotEBKK+UI/jCxmlFjwRFxOiYG0fyyAM1eykDm"
    "nlUwYDZL+alYtRAjgSZnJsCiiHMXzFBCpv4cpmw9xz/lh8MOF4HQ8tuQiefQSJYSgF7YWv0AAQAQ"
    "hTYsHJZhCt6BSsigaSo8OIIzfGKCYwQuHZwYjtYyXomOQQh08iDjmIiVgDpbYdjmXggoLAcRibmP"
    "bYT6NIbodwFaiLPcz95gtGaYC9iHQnJ6O68ECjemf3hB+KsRHAEGTUS3Ec39PaOIBYd+euZ+0AQs"
    "SOknuHQgUnrSHrB/1LjfAETeEmSleHqC2bHhQ8+1yJDAq8jHlK8f9gqcESyiReD/ALtrAsI4FYgL"
    "EkRg5MAQQe6JQlx7wV/zFZOIWCuOoYhuMVOwch+gWBpBhSdDdyOMFbCYOAcwLFBPZBt9hXC7Lswg"
    "qhsbCc65HYrgBP8A4f1DxKxwJH98UIjHCXhySxfQCa76EQtOheAXJg1qb25Cp9B+yFNrmSWnabYJ"
    "7tnxbnwH1yzbDdb2ZeZDg8pe2D6tqtja8ECv8HIKkvIEcAjsGNxJJOJIVGmHDLOy8iIyQty2FdgO"
    "bA8gUCSPmgDwOcVDUUngmBwF6ApCwTkLQozORk5PiSMmABYtIbkEE3Rz8YCWDzfRpIsVe4twYUEj"
    "XaKc2aMcMbGQqZwjUauoBJxUNIwbUCQYiZTwR8KND6a3RsNEVAld5AaJU+sH7Kh7CyYrUKeRnzvB"
    "QWZ8nj0gwSp44a6SPcBgftMC/kN2kl6ZASSMBNyEvskoJhMCe2Z12xiIHJ6DkfjQhSJ2MbCQwhTi"
    "oNsNIjQh2kLcIeGyFQFhsFtCbwjg0sBYEI0cfdKkZDA4cWB+XvfoUFP4RvsDKBJujjSA/Pflgchs"
    "IYNTBqRRY9RJ5pQCXYBGQvlw30OYT5DcPJepA7ZCJDEjjF5h9w98YEjCBCzCdMFmIkDgEOIaERsy"
    "EC4EslRNCRMW3A11CMPsxEFYcp7jmwH80KTBhMPsOEOSDjE4ThOlpE59xEEnAnyD8A+c5h7CBaYv"
    "0kPiIB7DwQfooLImwhiD0LDWcKXuYooQsLD1Q+ljs0wHzNyxQaJg4gG7icb6ZQy4PsDEmx7BfgBz"
    "WKzBRwCbciGBxKqCDnSNUY0vYqT2Q3gQBXuEDBhFFekigGvUENWIigtgoCpl6HBdkNBMC4DzMT5T"
    "kR/gDvh5exCgy5Jgr9kz54OMEl5EMjwASSAitEyfk0AwsicBQQJ0JUkHlnNBAc5gUhxgOSZBKkBm"
    "Ca0TA1AKOBJ0GuScFoFEYKbG5yoLqF8gfCMoVI3bFtJe/pUF6yKXfwFk3yPoxgqz5FTBzksBIhEJ"
    "wt8BdeWxhOx4cGBydAbTgBDg8iZMiScQgXGwHGw8oVaQLBZEveKYBiaxOoDxSBqJPYskcXsRoL5H"
    "sIcBeAgMFYKIESSWA0YNMobIIwovI6TJoPbEIM0eLsFqgQ+JBCqgtoLzwUFbY92AKRwBTYGYLA9T"
    "YQgT2EaJaDr7sPpDop8B8oNhXPIFk97PcOTqJFL2PdtHA2MKDkTLBHIL0HOWb8DRA7IFLuFzuAL4"
    "KwoC+/BG4QKTsBiAqMgCyRUwVvQNIG6QwGoTng/ICX+QSMCTrAai3DMz64BXxnFdTkAVAo2rLMNV"
    "nSAYpEqMGyrekQdgHrGqkRIQW+qtyph0YJYAEbG1RSg4PbPlROUBEv78pCoBsSlbkzAk/dCHQCDf"
    "gPqVENgjHBlXr4RJCVFTYwbdfWAoJgq7xGmRo0amR+IgYEfQ4Vn6ADd8MiRiav4RFF6tvUfzAFCJ"
    "PJikAWcFY0c8NpRDAlAHNoLDAnOCWPqi4FkPRkiA2fqGHDgHDCI0jD0bMAc49eJkEZ4YgXqIqMfo"
    "S86i0BzCBcLT+HqHbEmhmxTRE6sTArqxcDq8KArTc9SvU/cbAaePylArn5X+p4qL/ggFufEaoQWP"
    "L740u6lyOzEteIkb6kJ8bhT4b6Lzfmx+Cn6SM9erjrdsAA8G/wDYh8i/hmLMUf8AeAG2VGrmvMPq"
    "Boe1idp4dwHn0PKrBkX28ol5R/Ahw1NEmjcohwBZOrM9LZ4jkf1G/wAFRFRLj38ivoDavDJQQWOY"
    "I6/pOFwhgP6yDIDMSwezY0ov6m1HQvPFOrcmotorAS4QCYKQsnTAjU4R+AtP0ZKgpICgfIu4mYBv"
    "aYKXAVRiuIPcfWYccogQ0EvD0ChsOYg2BOsPXmCjsw6QE9gO/GCDgsYsZM4EmJ+Q1D70P3Hr8SYP"
    "mfhA89fsKvYyg3U9dIIrrKBJm2AjrxRIW1hmGn+7wCffAfQAdfBL9pBc6ttwCXvCWMOFAGYLv7sY"
    "d4xzBUh8YC1m20DveAUN78oDFTGhSNVhlNg7CDQ+kd2L2BYHZsqIscjg8lymCVQRVBYQB0jsC6HH"
    "CAK2F6DweOCDLgMFVyJg98Iqn/fjEF07PhABUxdJEk170RAvqJaujgD3gRV20F0Ar/NFPGp6GG3e"
    "AfrEeBxmxuJXbK1ZDyZGRfHV2wLoPMPQRMwiZBAiFeBk7w60wdEPKUJQIAYY3nj5MBkeRKdQxJig"
    "ExHwZ4scQqMPRgZPqKjc36SeQh9QkAsKclwaNAeC9EhGhwI4EffzNAJcom++M2BXiBGyoNiFI/S8"
    "7QIpgjOJOb3MEd8o0CXrc2Wj2QNOnXxX9CAoKr1vOzOwM/PY4vcJdiNChf8AscrY+BDhTb3cATRr"
    "NH5V8hOdA9ES/nk0LMfW6sySoBq4MarRgDkJKJAH2O+/8tg1/bJt+9FCfnHo1YB/vukAQxMJCsSr"
    "wK1k1kU53KLR/KxPqZD6yiji+4GGpRJfVcguhDtkMPrICX3BPkgrT4BYCCZgIGA1YHM499jBamTq"
    "OBW9NwPBsxDw8/uJ8C6AKuFu82kyD6h2j520DWsFtCAxggt5q8w/symaqQX97ft/kbV29ayDQP4R"
    "zuIQpKUfM7f8DVcheEVveDmu9OsBl6xu+xwgEIWLFezHyFSW3/HG/wABgbOupfkxzoBf4P2RjyGq"
    "1YvUOaAe2uShBAV1EfFngxU6cLlTcxMeBIawTeaMgrjAtX5QKDeENECrQECBHJLQy/8AoCCM3k30"
    "IwiH98sSf4wDkmX3GOxR8RBIG0oYOPyACTeMYSAFOOmT9v8Aa9jDiCF10BvEI8yOuH8GBHOHIg2x"
    "PUrBWpsREjwRKR6ocgPQiBA0FoGD6FYLBjkVnkqwaYec9AR8X1FElwZhudoD/CZ+IYrAx3sBvYaD"
    "DyWQ5/RmJPqq7oYCGPkSj/kH79QEdBUi9qYT7pVj6wIXR/484QSdKoHCQyobn6tnJ91HJORJIwjb"
    "+Hh/BDRF5mv+zwDoRUhRYeZgJ7EfnA067hAa7mCnOaEfgCbWP0TBYED+r52hfDobRmCATo1jDq7S"
    "OAG5qgo4GmHA29C2kZziouBh8IbGAs/SoKLAXjAkMLsFhNxfpMLYeJCNaiSE03/Ly3Z4BPp0gDj+"
    "lOln6SF9tz57A8QR36YlYAFezOkEAD6cinnUFhEsZR74AEhcC0v5XgOc7gFitj6FffCcmpyDTPjh"
    "X6wt71dxRymmWDAOlstgQEYKhha6og4UdMEJRRpC56PiCMATD6gHgP3B7Oy39iEBfBP64ImACUBA"
    "qtqb6AFwuqEeyCMGE/8AddqELaYL+zGmigjFa6cQaE3nQuQR0Dfas5CpAiZkz1fwAIgoGwiItjaM"
    "MSHlf4HQLPTmFlgQxExbUDLAeqCAD9JDssKCgj50FBZhiCFJ4HSG7Ary/wCKXtG/630Yc+mYKW+E"
    "90b7uOrhz9nYIcefCK/mjeAHpkQfDhPbgNwCH+opKKARmQ91Dh33Fke+sEK6+FGXGAMt7wZHizzw"
    "HSNkFewQgrw+J5pVdhjltDsJnOhGKxkzWSDf1jL0gxpiNsPKkCF4QTtBYOY0JEiB4KlBawQNkHF6"
    "g1oJcBCl4mxe9AsQl7bRXsMMIVGw2EbSTN60Ic5mmYa2xOAWecCWj0TBI+smeCEENSoknuF5+NtL"
    "3Q4pycDoFoZQCimHJ+xk/gnxMOue6FqnPxiEHUOt37I+ej2IWmoHoV4MPMro4AIuMPBgJUnummVg"
    "YFMsENFLlROyXsDU60g8QAAFcPKTE0hAGRzbE7djAmaKSY3PbjVgV2aJHwvKCJmIh0YaetIIVooH"
    "gugqEQXBcFJjEPNsC9BBD6JoPAdsVOcJoTr3G20Frg34xhd+EKJCHmfOhhiwAmfmFIXYwmEjmWcY"
    "Dr0Q0DEaFKgBBN6wBbPDHGUFQsNBTzEZiBht+CahhHEhYHYIIEPCGnpUWAI5LMFPqDCQeoBhGAed"
    "kg64VMIJ9sETgKiA4Tmh8WILkRQTbuAk6wlhGpsL2xYCwShnSGx6UlbZVghYNwMnxqRhaDRxmBEx"
    "CjJAZBSmBAgkQYiDGuEHgQmJFxn/AJi4UjtrBT3XDTyCw9wHTAQHYdjIcKTBwX9NIXfJcZZ7E8xG"
    "yOQRc0Bsib8hBgi1mQDkPYBwVbw8tEaYcRgwsPICL2GCqnb0xQVQ+RiweEgRmFJh4KwqDZ5IhiOa"
    "gEqdAyVGgcGCPYjHkICOTAgWsq/YZKwD5eKYo6C3yGB7Y6zEZoOlj+Yx9bA4248MSngdL0EEHaQQ"
    "9EwPiHMKnIOYnwhlwsh3PQEEjDb0s4xK2GgeQuMGUOILBavWGEgSxTiSrEQCv2DoL3cETClAbYDc"
    "dQgWY1+5iOICsHgHABkwCyYGL8IFOUNsP00QR8hxwe3nJtiBbBSC4gc5qDyiSfRoXeBfr8g71D1l"
    "ekDwEKxwjKEIhPAxRBgj6hIwAd5ABPB0Ig2YBtEFzEKGBAWuQigQmFXQV3wn1AKMs8Ka08g3QDWA"
    "cU0b9GIFBAmItOQeZwsJdgmnYMuADk9IThC6SB02nQHPJQ8aodmBWiOBIjKpa02vw5JyupVtpg/i"
    "fIKHARPIwaBDWJCxzVvQ0K+nCkvsAEwxYikvIBciv2UNKTaPD6SbyL6k/UQ0LYWcDiJB40+0FwAO"
    "xeRh19AwEDD1xmWsftgP5dov5g5gzimdj7gFuSIK2QPcq8AXbeQFioW7a9kDkI/loRDtogDGvxQS"
    "DKDgfh/47uQqy/iKK8kBHf318AhAo4YqaXJAggztwmAe+przXSCATjvtCqCHGpwh/AB6MFkPeQ15"
    "B2Q1PAZbVgE5Og50BOMWUF0HAENcYMQSCsxiB4A5Fg/RYR4eAUkCG2YEEHVFoXVIC9w3t741hUDP"
    "FexotvWwF4vF70XpKlKbFe/GhpMD1iaY3Kmg+s62/iUhl7dkaPYyDa7fad6fcJCEgovahD/htbR+"
    "ARvOZtF8YBPPV/WLgSBp1fJmwECAhSzELfCmj6iEF+mBP7GHbVb0oWIDg4N3g3I0/wBCEAA/y8uS"
    "9+V1BB4UTqlFyuOuDXlwSYQAF7QmK19SQmRpp+ScDa+kTPbbwCpcrAUUAAa/lLIMJJOXviWhRtiO"
    "bVn4K8g8vFw/kAgu98EIfiCgcCZIbqzsnODcSEthAricWPrF4vsA5Qgp3wNA/RPBqEZyFXBIcnoF"
    "gvgH6OcCyXNYWp7JCCBAveBEzgY4Bgw5Hxux6lMD77ELg55BFxDco+D6EwoD0wkLsUYA/M4nVgR2"
    "BpMA/IExFD3FAehQOKUg3bNB+k0HBmU1nZCP+SWcgwxYmJ3xLtyDDAZyF22eAaL3pozIFeEwIgae"
    "h0xhBkztzgn7nQ1vk3czxIxRAepTmA4PwCf8QFaz9ewOCAMbmOlqeVTCMSb/AIyNgAhKWP8A4NeG"
    "FRGSFk91D16t7GCInDivrQDy76LU7wYB17hp7yMBLfZ6AlWA1ghV5y9MfWxXYkF1hkI4MB+yCeV5"
    "f0MDCfhpFbKEAApKcxfRajATmFS+HQcZ9p5B2qACDTlaiEwME0XQ7XP6GMMQJiwEAKMRYu1BbPsI"
    "kHgheg4n0OuC/wDoGjsRfdSAhgdV5m7D38DBxoROloIjV8jQReBm5yCBMOe/tCCD4t5aFifgJRbE"
    "I3wLITMG1eEfWQVc5hcfsmCmU5LkPamDkU8it/N6BGvWHQ6QGodZDaSfOKP+BRcOkAggJrsIiYsZ"
    "5l4WXRGhMD2aSjvyHtkCew9AF21e7u6HIDFNsm6UROgnaGS7wieQ3ehYp8isPAf2QB8vgJmvQlM9"
    "MsgjALBms+0ZqJ15FB3Q+/oPAHd6koaeCwFpDXh/fpQEaKZ4MUEG8l7LC+A9/TNlYCPbK/jsBGF+"
    "t3VFJpAEqvZkMu3XBxtNtEBZGuUkGEnJYEGC2gmhHBI9wnFCSRCBDCboutyfAB46di6j96DaglsG"
    "rpXQ8Ob/ABDi5tQ/nAq0CotBZKA/M3qD0gEP170UvIP6D+HbsGhH9FH4Z+4Yj3kkDqZ5xfhBEVM+"
    "tL4E/HJdq0iqgu+tT/mYEGBG5p7iAIqSY3ab6wgdIHS6AL+4BTxiKc4khMwuAnlFMecHMHzr3lgS"
    "xgz7R+oaB4SacYD62/m3cBu/ZTvnzYOER5uOCBndsKnfaTElS1Jid7wAmt724gGzxuUkItA+1240"
    "CDRatTXjpSFXchuFQigSj83gDgYBx/LekOgCPLU90oF/Iphwt+A5Ze19mgXYA41vaRxL5CgTFpYc"
    "mCAGPSquEsaSv8AtTw19zL4aAat+kIAB76RdxFQDVWT1eN9GuAd4ySvc08WpPI72oTfIgI5HPniA"
    "WUBAvSJwgDwvUwfuxjhvlQdLck7II3rfMJd/4Yj4IodAiBfoZDvH7UAWfwH1XxL/AImHkwE3ua+B"
    "PxwSJP8AAakBbl5MG0h9TcwT5lUNi3sD8MC5ZdHf3gl1yHrcE0HZ/SJipuKvYwVSFtK8njyOSH3G"
    "X7WB2OFvpgFCoWgQNJoA6CW0+0QOnQtyUSTQAOzV041bWpgOZ9yG0qWwaWMJ9a5gmaAEKNAYV9kU"
    "R2eRN07EwFTR6beAHXsF/CeH2VyAG8YpDbgohgAakOm4gC6j/F5EWXULDVmAFMDXCGnIPkysUr4S"
    "LffQ97dXsGDQwk00r2yKBWvvcWvsBOLFpd16vQJY2EQtKXas/DCJu8VX4BgG/ctv9uYBxu4C2VwB"
    "/wAcMsmARjqf4F8a3B3Rx3yBo76ncvI2ak7AYELdkn+XhTbAO8WruJa1kaw5I38/9ZwIWx+8d7u4"
    "D3rDgdwSzb/FE2yFr4Lb5BpG2fkegJIl3N+ieJgIwopEbs3t7Av3QT+vI4c/ifAqtpYIiUCbGTdq"
    "fdcAJKVj1EXnKmhsioIMFSkqVKA+IErJXiEfGErA+fyT04GIgPdryNGSeQ/nUdMEp5CX7CD7sl9o"
    "DWSN7HIUCAHbullWyeQQL/y5s6PEAHdobPJ5sa4GAb/zhbIA2AnP2im8gAh/XSIL9HqwRZxDkCzj"
    "8nwXDpCTGcUMG3BjodHoCiHJgON2AUe7Rm4HfeCBJxB8FjpSVilPZwP7Jrg+lUJIIWDY904WfNwU"
    "v8ZdWDwSfO7KL+Dj8dBeOtdYIxpGgDXKRXaqyB7fm5/Ogc2fAGf5DxvvECIzVgB5P4BLvuMg2DW4"
    "f5QId+7MyduCd4SJWHBgjE+lmGhS207gHNRE8g6ihtQArBT+99VA9mvuNMB1GxQBi++35gIFDVbM"
    "kcAm9UgqCfpK6GPiC9Bk/dE6Aej1MSgsQTANWmWFMti8GsB504EMGNYcisMvhr6U+aBkTdlqbs4Q"
    "2sIGScCMdb3Og+Q+egHnT/zAcHxI0c/ZsRKWr1rwFbOwZ+PzewENn0O1FtBYFAaiXueJA4OpysXP"
    "sdDiSfnUfYBp0DRhqCAp4Otj1Hhicrqmy9odNGJTXeeyQEXkPfcDQBSk0fTa6J4EG9nP5u2wl40z"
    "2ZS7IUnEXVDcF3Ck4DYWxcQkkA3MASYgdwETOJclq2BTKSw0Cai2KMN2YBUHAA/NxE1zGo6cBp2z"
    "CvaILs4nO3ACn4COvveOeXM/BIBhthQrscAPRgEICQbcJ5aiE4HAFNhBADbIrhIaTku9Asl4DwEx"
    "jAs9IDmyBhlQKIOcUgFwxBRLuLBY0qGkgC7GkbgpacBZWNh6iTOIWAHzAHgWF5uMo5DxgGojDYqg"
    "i+hYcVgUPEnEZFhxEsTngP0hLCwB5BViIZCpkdgTYX2sAD40UUUjtqYeDmIOYJBKAKH5Qu0GCTCC"
    "TYNTE+hh+cQx7ewB0JsYuIXzEkA8PFMBRJDAEKNCEzOFxkOxHiHxvQcEmCcNNejBA19C6I2CwsdM"
    "BCUeA/QIPKoOwX8gMw+iQ7rAHyDwGvgLqQmMgYYorQ0A4gRgxArB2QUmKRvI8MLEIsCkBCuQWDyJ"
    "gs4BIF6uBVUZvggHnBXE1NhL7aNAkBM0yMA4MB4K2S6FgAtBYMaDNsRCIIwr3AcGCJ4romFuLDIT"
    "6cSD+yTEwF+5dB09i3nQLBF0QdBe4K4GUYYxX4RxelZ5qgHGEnoAMOTBB4ERebAYrGgz+hG5R4CI"
    "b9iZ6AF4CwpyBnAiR4aYTBaI70AoEBDMHkXxIKgewSxJb+mECa0kXoqIIKOddi1h0FAywHYBPrDI"
    "3CxGcGSgKHCMbEX9w+UhwQJ+sDCJnsPEe5AQ9mathWGsZJgJL7AhIq9I9N4jyqYDxAo5CXCIQBd9"
    "wMZyi2C9xNBJgQxG+BN4BcEqocgUAskjExhHPId2Dq7BRmRclACRcgmeBngF8wkTDoKrJPg8YLCk"
    "CusBQmBawcwhyBSw8H6H/gA6QAwvKxmwKtkT9eRKreRE90GDyOP853RDWcBGuAqA4DQihyZCRYJh"
    "etDmgezBDyEoBYEwQi0ngMBQYX1CsEqwJBCKQXSG2HbN4EriIJ5DNhDsIHk6Y5GIygQeUB6ZNZBy"
    "5CkiE9gcBKI4wFLfxgURAcglr+uNTAoSchbBDcwzrDKSYxYQpYLB2Qn/AEg4CFiMBC4HJAIPx6se"
    "CX1F44CEoVhEHfC+MjG4Q06AaGSXy2rSTsDqz8IwAmt84V1WhgTfQsCTghlAjrLTkTWzyu02cQeB"
    "LIBHgATNp15giGAaD5mSWE2ewyjPyAzZU6MN8wM14ygiboeUi+/xzOOIqenhQAFyuG0ho0pi9MI7"
    "YQ+IyUwIgAb6mM4goTdRdTacgV02A6HIwkTLkg9fZCAQO+RSgMGaTMTncQAagarCEChGfTEVRXuT"
    "u4C1CyvXwEzYx51WDcgJ4IzHEgAGtW4+OYCo5bTRCIAk3WR8giz1Yg8HIGb1uzZJkAUqNzbZ4whz"
    "pqCglN9A8ykw6QAq7Jh1mPxgchogopKLMQI6gU4GieiWJhpjIQ+/gkFa0Gw7E8HaHyYRF9Qf8AhE"
    "YFgW5U81g+CHvu+lWQKIvu0WtBBncHgRq0jQK7udiZUvQrZokZff+MiVWnWV02CBeRHIq3j4FVy1"
    "aABpYlfi27DYy6BfqSuPfMFNYyDJNyAhUHGM2fgIq3j7LdgYVNKn6/XSWZMYhsviDbi7aeaxoB+w"
    "DhOQ7YX7amUAW54DLWV00gbn0C0PooEKfFEiCgvkefI3UgB2oio9kIlCB0hx6osDCP24lJMBs8W8"
    "xHmN/wCyqEBXpXYDoD/mrCkKlE7AfWkCt++AL1ZHKJdHoB+kTJiUBLCQcZ4Kgs1iEEvEAA7jveVQ"
    "BIjBDpsxYPpu9SyEyCQgCksXh2wJ2FxbAjQjagH88mOtQ5TwCmwQUwUc9BawI/AOWJtjKAiwbpv8"
    "RPbwIXoF/AMQ/bi9+hh+dodH1gAetGQBq2j4pL0AAP8AbOwR5a0lUmlOgAMs+9/mB7zSMJVWAegK"
    "wBoBwwQCVVgXsAtPYfWKawUpAnPewghAguwrtsll4WIIbeCdGggQW2G0hSThcEP65ZIiXagoh3AP"
    "WDXIFgzdBWhQA3WVlZTecOVkf19O2hAaNeElmM9RYJ0EG9BW2YH5EQJo6obfBoGlWMG3zUgvmI1p"
    "bSvPGIukY4hjXn2xGoD6zo3d0Cclg1EMDLCQ0yefkUuR4ge6RF3EIAjIo1FlF+5wi0gH9iAIGyaE"
    "G18A/oNVg5TCAFG+I41gbSweYFhU248g5rA1lHYAN+O0rNLQDKDnzoIPFzjpMItB6hcW6A4HbHOM"
    "OmxyDQUhXBV2x49GFwjTPUCJrzQI5sESebeR5QJw0GFTBkDoSC0ahZkWBCWBQmfpeh8Q0ojJKEWc"
    "XFrm2EAbC5d6KTkLJDzfKVyAv84bbvwREs7L15SQDtXl1FGoLR7bD6TiNT03G7zTN5gEuPbvCZyF"
    "kiMktg8LwTcgyOlqvIduEWBvrpX+PVgE+2SP2tQYaCoi/c0BunB64gcoml59gAC5HYa1nkg1l3ra"
    "d/8AqRolvK7/AOHBJKSwKPT/AP5bcLqgeCOaBkYqbD3J7gXgSBq4AgqMjpFxZCFOCXnw5E7EuQQJ"
    "Exgl2ecPBrh4KF8BWOFZgrPJGGwkQnhuwTv9rBzW7WwOJ34IdOMcBYX3vaIC5AFsJ75TYDylqbHg"
    "DyOpC2g3Ibc/MRp+Y+X2FCdWhD+ogNd10ZcDuX9yBXju5BVpaX4GIDfnc/ME7mk0YklA3nSxyKKf"
    "RVKowgsNbmXB9Bikkg43Ni1oHY/lzSA9LQbPAsGiUGK7CJgVW8YyPQdBU9IBcPcKAoJID1AbZ4sP"
    "AOg7ZYsphB54PePQQnbAHJEXyLpj0oAn9MNrhJWFgMsWSAXegkVkeTtkbvkAgbxHg4eg8Cwkwsat"
    "guBx90B7zxkW0cjCwEzHHqLsBb/yrCIJUVEE4KopQqzgcg28CcscGQVz6IQvQPSB7+HoVhNcAOwf"
    "yYqwX+IiBcDQVP4Fec0NBB4eIOPUeRWONeUsHSVyV42CMUtWAvDQauBGp8iYE5EgCBA5xoPWCgbk"
    "TMT4IHiB4lDnI8LX5PSH6SJgoQ/WQUBpQegWBEYWDGLC9CGvQaqKjhltBJEB0EBHbwTLE49EgSRg"
    "8sPTZFwqC3CD2gfEwEBiaF2IcnnQm8AsRJznYOApBrCwXoDDD9CCVMFLyCaKm+AUNjhAv0TMDGFj"
    "iBgxUyqCFTUEowgmCGELkym0BdIH0xGhVcCx9CEe7FZRIOksjkbYLHCIENg2EJjwhJ5RBQWoKC1j"
    "jLw8jJwpAlQMTl92AOIFo1AZxmoH6GNR7xsO4w8+ysAL947gSg6yIOUNBH7HYHoaKUzUL7Ak/QEQ"
    "OVhxjdPkTLygRUCVki94MUgoJwVAWSBC9Y7zgsWgkCUbuPsOqBf6AwBYDDEPoYsGpe2HmsCg+Awy"
    "j9hgcAZlkYJNCc9A58LNjUve4+hExYglDUvwRR9EiHywIgYYdhDwVMO5kRgQX+IIPAknBkQ0Adg1"
    "BhVifUGEJ+HHacsDA25PQIkkN+EoBxIwQRaRoIceSCoN1AyGEIOheBA8dhDzaCC+rEYneMJ/wgLB"
    "dYXgQnAbICnhO8jBsgsH6FacrLL1KonAqw2PjEmwg8DwipwLAl+X7iYQ7BYUkiHQfoMeBQsTJKg6"
    "Doe2Z2EGbZUALALAXAbgjA6CoKkKA8R4fpCUVgQumJQ4QEspcBwYBDTKCIYdsFqcPgl8mFLkj0Do"
    "9JoY8WCnkEwbCgQeWMWDYQVhFegQS1CwNMg+h2xHsIbDyM2HmIXBD4xgOSAX8CofGwcmCw0KAsOI"
    "GckSoEr0ZVQLIHXGDkv0igEFEYKgp4DxHhYYggQ/SSgtPQEdwVCwQJT2RCyLsVzhNE/jga5DsLIh"
    "BYFY4BMw2HAR+z/g8g37P+D9n/B+x/g/Q/wWT+v7H7/+MdW/Y/wfs/4P2f8AB+h/g/Q/wfof4PMN"
    "+x/g/Q/wJf6/2P2f8H7/APjGb9//AAfof4P2f8H7H+D9j/B+h/g/Q/wfof4P2P8AB+z/AIP2P8H7"
    "P+D9j/B+h/g/Q/wfof4P0P8AHo3Ds36H+D9//B+r/g/f/wAYUt+z/g/Y/wAH7P8Ag/Y/wfs/4P2P"
    "8H6H+CYujPY/d8YDfof4P0P8H6H+D9n/AAeRjBv2P8H7P+BNSv0/BfP6/sfs/wCD9D/B+h/g/Q/w"
    "fof4/wD8jzTopNLbekhGgX2rFjWslP8AJGa/01IPkL9yQ/qQiNjSt7B/UHJD3/qJhJlxE17mnmP9"
    "xaKrH0YWFd8Ukj9B/JaJHJJJv2/0kGJeudkbXKSd8Ino9U8eY28ia+cuGj+D8nAurEic+6WX3X+k"
    "llA+uD8EKEOmfCKceTlIhNxpJhezpf7n7vl/q3VbW0pmoS/jfuPsr3SBORS+WzSRCIoyRX2H/pE8"
    "9mZ5AQOiatdpEbLNHBS0fNpITkK0E+T/AHDpjA1Dj5ItHbNRuVOD90PYk3Aiqy/Pjn/RdUifyHuk"
    "2lrsm3dEA0WxRL+Rf7jsulxwJwi4S/0+MAK0r60Mi9b8ojt4eT/8ff/Z"
)


def kb_categories(cart_count: int = 0) -> InlineKeyboardMarkup:
    rows = [[_btn(f"{c['emoji']} {c['name']}", f"c:{c['id']}")] for c in MENU["categories"]]
    cart_label = BTN_CART + (f" ({cart_count})" if cart_count else "")
    rows.append([_btn("🔥 Выгода", "info:benefit"), _btn("🎁 Заказ по акции", "p:home")])
    rows.append([_btn("📜 Правила", "info:rules")])
    rows.append([_btn(cart_label, "cv")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_info_photo(promo: bool = False) -> InlineKeyboardMarkup:
    """Под фотоакцией/правилами — «Назад» (удаляет фото, меню остаётся);
    под «Выгодой» ещё и «Заказать по акции»."""
    rows = []
    if promo:
        rows.append([_btn("🎁 Заказать по акции", "p:new")])
    rows.append([_btn("🔙 Назад", "info:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_category(cat_id: str, cart_count: int = 0) -> InlineKeyboardMarkup:
    cat = f_category(cat_id) or {"items": []}
    reserved = reserved_snapshot()
    rows = [
        [_btn(f"＋ {it['name']} — {_fmt_money(it['price'])}", f"a:{it['id']}")]
        for it in cat["items"]
        if item_in_stock(it["id"], reserved)  # распроданное скрываем
    ]
    cart_label = BTN_CART + (f" ({cart_count})" if cart_count else "")
    rows.append([_btn(cart_label, "cv")])
    rows.append([_btn("🔙 Назад", "m")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_cart(cart: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(len(cart)):
        rows.append(
            [
                _btn("➖", f"cd:{idx}"),
                _btn(f"{cart[idx]['qty']} шт.", "noop"),
                _btn("➕", f"ci:{idx}"),
                _btn("🗑", f"cr:{idx}"),
            ]
        )
    rows.append([_btn("✅ Оформить заказ", "co")])
    rows.append([_btn("🗑 Очистить корзину", "cx")])
    rows.append([_btn("🔙 В меню", "m")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✅ Отправить заказ", "so")],
            [_btn("🔙 Назад к корзине", "cb")],
        ]
    )


def kb_status(order_id: int) -> InlineKeyboardMarkup:
    order = db_get_order(order_id)
    if order and order["status"] == "paid":
        # Оплачено — рабочие кнопки статусов скрыты, доступен только возврат
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [_btn("↩️ Оформить возврат", f"st:{order_id}:cancelled")]
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(STATUSES["accepted"], f"st:{order_id}:accepted"),
                _btn(STATUSES["ready"], f"st:{order_id}:ready"),
            ],
            [
                _btn(STATUSES["issued"], f"st:{order_id}:issued"),
                _btn(STATUSES["paid"], f"st:{order_id}:paid"),
            ],
            [_btn(STATUSES["cancelled"], f"st:{order_id}:cancelled")],
        ]
    )


def kb_pay(order_id: int) -> InlineKeyboardMarkup:
    """Кнопки уточнения способа оплаты (после нажатия «Оплачен»).
    Если у гостя есть баллы — можно отложить их списание в этот чек."""
    rows = [
        [
            _btn(PAY_BUTTONS["cash"], f"pay:{order_id}:cash"),
            _btn(PAY_BUTTONS["card"], f"pay:{order_id}:card"),
        ],
        [_btn(PAY_BUTTONS["transfer"], f"pay:{order_id}:transfer")],
    ]
    order = db_get_order(order_id)
    if order and order["status"] != "paid":
        pending = int(order.get("bonus_pending") or 0)
        if pending:
            rows.append([_btn(f"↩️ Не списывать баллы ({_fmt_pts(pending)})", f"pay:{order_id}:nobonus")])
        else:
            avail = bonus_available_for_order(order)
            if avail > 0:
                rows.append([_btn(f"🎁 Списать баллы: −{_fmt_pts(avail)}", f"pay:{order_id}:bonus")])
    rows.append([_btn("🔙 Назад", f"pay:{order_id}:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =============================== ХЕНДЕЛЕРЫ ==================================

router = Router()


class OrderFSM(StatesGroup):
    table = State()     # гость вводит номер стола
    phone = State()     # гость вводит телефон карты лояльности
    rsv_name = State()  # гость вводит имя, на кого бронь


# ------------------------------------------------ утилиты гостя --------------
async def _cart(state: FSMContext) -> list[dict]:
    return (await state.get_data()).get("cart", [])


async def _table(state: FSMContext) -> str | None:
    return (await state.get_data()).get("table")


def _cart_count(cart: list[dict]) -> int:
    return sum(i["qty"] for i in cart)


def _cart_total(cart: list[dict]) -> int:
    return sum(i["price"] * i["qty"] for i in cart)


async def _show_menu(target, state: FSMContext, edit: bool = False) -> None:
    cart = await _cart(state)
    text = "📋 Выберите раздел:"
    kb = kb_categories(_cart_count(cart))
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    else:
        await target.answer(text, reply_markup=kb)


# ============================ ГРАФИКИ (PNG) =================================
# Картинки к /stats и /loyalty. matplotlib импортируется лениво при первом
# графике (не тратит память на старте). Нет библиотеки или CHARTS=0 —
# бот просто присылает текст, как раньше. Рисуем без pyplot (Figure + Agg):
# потокобезопасно, без глобального состояния; рендер — в отдельном потоке.
CHARTS_ENABLED = os.getenv("CHARTS", "1").strip() not in ("0", "false", "no", "off")
_MPL: Any = None                      # None — ещё не пробовали; False — недоступно
_CHART_LOCK = asyncio.Lock()          # один рендер за раз (экономим память/CPU)
_CHART_MSGS: dict[tuple[int, int], int] = {}  # (chat, id текста) → id фото с графиком
_CH_BG, _CH_PANEL, _CH_FG, _CH_MUTED = "#15151c", "#20202b", "#ECECF1", "#9a9aae"
_CH_GRID = "#2c2c3a"
_CH_COLORS = ("#ff9f43", "#4ecdc4", "#a29bfe", "#ff6b81", "#feca57", "#54a0ff",
              "#1dd1a1", "#c8d6e5")


def _mpl():
    """(Figure, FigureCanvasAgg) или False, если графики недоступны."""
    global _MPL
    if _MPL is None:
        if not CHARTS_ENABLED:
            _MPL = False
            return _MPL
        try:
            from matplotlib.backends.backend_agg import FigureCanvasAgg
            from matplotlib.figure import Figure

            _MPL = (Figure, FigureCanvasAgg)
        except Exception as e:  # noqa: BLE001 — нет пакета/битая установка
            logger.warning("Графики выключены: matplotlib недоступен (%s). "
                           "Добавьте matplotlib в requirements.txt", e)
            _MPL = False
    return _MPL


def _plain(s: Any, n: int = 24) -> str:
    """Подпись для картинки: без эмодзи (в шрифте их нет) и не длиннее n."""
    t = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F\u200D]", "", str(s or ""))
    t = " ".join(t.split()) or "—"
    if len(t) <= n:
        return t
    # объём/вес в конце сохраняем: «Вода с газом… 0,65 л» ≠ «… 0,5 л»
    m = re.search(r"\s(\d+(?:[.,]\d+)?\s?(?:л|мл|г|кг|шт\.?))$", t)
    if m and len(m.group(1)) < n - 8:
        tail = m.group(1)
        return t[: n - len(tail) - 2].rstrip(" /,") + "… " + tail
    return t[: n - 1].rstrip() + "…"


def _money_short(n: float) -> str:
    n = int(round(n or 0))
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}".replace(".", ",").replace(",0", "") + " млн"
    if abs(n) >= 10_000:
        return f"{n / 1000:.0f} тыс"
    return f"{n:,}".replace(",", " ")


def _ch_new_fig(height: float = 11.6):
    Figure, Canvas = _MPL
    fig = Figure(figsize=(10, height), dpi=110, facecolor=_CH_BG)
    Canvas(fig)
    return fig


def _ch_png(fig) -> bytes:
    import io

    buf = io.BytesIO()
    fig.canvas.print_png(buf)
    return buf.getvalue()


def _ch_title(fig, title: str, sub: str) -> None:
    fig.text(0.04, 0.962, title, color=_CH_FG, fontsize=21, fontweight="bold", va="center")
    fig.text(0.96, 0.962, sub, color=_CH_MUTED, fontsize=13, ha="right", va="center")


def _ch_tiles(fig, tiles: list[tuple[str, str, str]], top: float = 0.925,
              row_h: float = 0.085, gap: float = 0.012) -> float:
    """Плитки KPI по 3 в ряд: (подпись, значение, мелкая приписка). → нижняя граница."""
    from matplotlib.patches import FancyBboxPatch

    cols, x0, w_all = 3, 0.04, 0.92
    w = (w_all - gap * (cols - 1)) / cols
    y = top
    for i, (label, value, note) in enumerate(tiles):
        c = i % cols
        if c == 0 and i:
            y -= row_h + gap
        x = x0 + c * (w + gap)
        fig.add_artist(FancyBboxPatch(
            (x, y - row_h), w, row_h, boxstyle="round,pad=0,rounding_size=0.012",
            transform=fig.transFigure, facecolor=_CH_PANEL, edgecolor="none"))
        fig.add_artist(FancyBboxPatch(
            (x, y - row_h), 0.006, row_h, boxstyle="square,pad=0",
            transform=fig.transFigure, facecolor=_CH_COLORS[i % len(_CH_COLORS)],
            edgecolor="none"))
        fig.text(x + 0.022, y - 0.022, label, color=_CH_MUTED, fontsize=11, va="center")
        fig.text(x + 0.022, y - 0.052, value, color=_CH_FG, fontsize=19,
                 fontweight="bold", va="center")
        if note:
            fig.text(x + 0.022, y - 0.074, note, color=_CH_MUTED, fontsize=9.5, va="center")
    return y - row_h


def _ch_axes(fig, rect, title: str):
    ax = fig.add_axes(rect, facecolor=_CH_BG)
    ax.set_title(title, color=_CH_FG, fontsize=13.5, fontweight="bold", loc="left", pad=10)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_CH_GRID)
    ax.tick_params(colors=_CH_MUTED, labelsize=9.5)
    return ax


def _ch_empty(ax, text: str = "нет данных за период") -> None:
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.text(0.5, 0.5, text, color=_CH_MUTED, fontsize=12, ha="center", va="center",
            transform=ax.transAxes)


def _ch_bars(ax, labels: list[str], values: list[float], color: str, money: bool = True) -> None:
    if not values or not any(values):
        _ch_empty(ax)
        return
    xs = list(range(len(values)))
    ax.bar(xs, values, color=color, width=0.72, zorder=3)
    ax.grid(axis="y", color=_CH_GRID, linewidth=0.8, zorder=0)
    ax.set_xlim(-0.6, len(values) - 0.4)
    step = max(1, (len(labels) + 13) // 14)        # не больше ~14 подписей
    ax.set_xticks(xs[::step])
    ax.set_xticklabels(labels[::step])
    ax.yaxis.set_major_formatter(
        __import__("matplotlib.ticker", fromlist=["FuncFormatter"]).FuncFormatter(
            lambda v, _p: _money_short(v) if money else f"{v:g}"))
    if len(values) <= 16:
        top = max(values)
        for x, v in zip(xs, values):
            if v:
                ax.text(x, v + top * 0.015, _money_short(v) if money else f"{v:g}",
                        color=_CH_FG, fontsize=8.5, ha="center", va="bottom")
    ax.set_ylim(0, max(values) * 1.14)


def _ch_hbars(ax, labels: list[str], values: list[float], color: str, fmt) -> None:
    if not values:
        _ch_empty(ax)
        return
    ys = list(range(len(values)))[::-1]
    ax.barh(ys, values, color=color, height=0.62, zorder=3)
    ax.set_yticks(ys)
    ax.set_yticklabels(labels, color=_CH_FG, fontsize=10)
    ax.set_xticks([])
    ax.spines["bottom"].set_visible(False)
    top = max(values) or 1
    for y, v in zip(ys, values):
        ax.text(v + top * 0.02, y, fmt(v), color=_CH_FG, fontsize=9.5, va="center")
    ax.set_xlim(0, top * 1.32)


def _ch_donut(ax, labels: list[str], values: list[float], center: str, sub: str = "") -> None:
    pairs = [(lbl, v) for lbl, v in zip(labels, values) if v > 0]
    if not pairs:
        _ch_empty(ax)
        return
    vals = [v for _, v in pairs]
    total = sum(vals)
    ax.pie(vals, colors=[_CH_COLORS[i % len(_CH_COLORS)] for i in range(len(vals))],
           startangle=90, counterclock=False,
           wedgeprops={"width": 0.36, "edgecolor": _CH_BG, "linewidth": 2})
    ax.set_aspect("equal")
    ax.text(0, 0.08, center, color=_CH_FG, fontsize=15, fontweight="bold", ha="center",
            va="center")
    if sub:
        ax.text(0, -0.16, sub, color=_CH_MUTED, fontsize=9.5, ha="center", va="center")
    leg = ax.legend([f"{lbl} — {round(100 * v / total)}%" for lbl, v in pairs],
                    loc="upper center", bbox_to_anchor=(0.5, 0.02), ncol=2,
                    frameon=False, fontsize=9.5, labelcolor=_CH_FG, handlelength=1.0,
                    columnspacing=1.2)
    for h in leg.legend_handles if hasattr(leg, "legend_handles") else leg.legendHandles:
        h.set_edgecolor("none")


def db_stats_series(d_from: str, d_to: str) -> tuple[list[str], list[int], str]:
    """Выручка оплаченных заказов по часам (один день) / дням / неделям / месяцам."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT created_at, total - bonus_used AS m FROM orders"
            " WHERE status = 'paid' AND substr(created_at, 1, 10) BETWEEN ? AND ?",
            (d_from, d_to),
        ).fetchall()
    if d_from == d_to:
        by_h: dict[int, int] = {}
        for r in rows:
            h = int(str(r["created_at"])[11:13])
            by_h[h] = by_h.get(h, 0) + int(r["m"] or 0)
        h0 = min([*by_h, 15])
        h1 = max([*by_h, 23])
        hours = list(range(h0, h1 + 1))
        return [f"{h:02d}" for h in hours], [by_h.get(h, 0) for h in hours], "по часам"
    d0 = datetime.strptime(d_from, "%Y-%m-%d").date()
    d1 = datetime.strptime(d_to, "%Y-%m-%d").date()
    n_days = (d1 - d0).days + 1
    by_d: dict[str, int] = {}
    for r in rows:
        k = str(r["created_at"])[:10]
        by_d[k] = by_d.get(k, 0) + int(r["m"] or 0)
    if n_days <= 45:
        days = [d0 + timedelta(days=i) for i in range(n_days)]
        return ([f"{d:%d.%m}" for d in days],
                [by_d.get(f"{d:%Y-%m-%d}", 0) for d in days], "по дням")
    if n_days <= 200:
        start = d0 - timedelta(days=d0.weekday())
        labels, vals = [], []
        while start <= d1:
            end = start + timedelta(days=6)
            labels.append(f"{max(start, d0):%d.%m}")
            vals.append(sum(v for k, v in by_d.items()
                            if f"{max(start, d0):%Y-%m-%d}" <= k <= f"{min(end, d1):%Y-%m-%d}"))
            start = end + timedelta(days=1)
        return labels, vals, "по неделям"
    months: list[str] = []
    y, m = d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return ([f"{_STATS_MONTHS[int(k[5:7]) - 1][:3]} {k[2:4]}" for k in months],
            [sum(v for kk, v in by_d.items() if kk[:7] == k) for k in months], "по месяцам")


def stats_chart_png(d_from: str, d_to: str) -> bytes:
    s = db_stats_range(d_from, d_to)
    cogs = db_cogs_range(d_from, d_to)
    labels, vals, by = db_stats_series(d_from, d_to)
    paid_n = int(s["by_status"].get("paid", 0))
    rent = sum(int(r["s"] or 0) for r in s.get("rsv_pay") or [])
    fig = _ch_new_fig()
    period = _ddmm(d_from) if d_from == d_to else f"{_ddmm(d_from)} — {_ddmm(d_to)}"
    _ch_title(fig, "Zig Zag · Статистика", period)
    bottom = _ch_tiles(fig, [
        ("Выручка", _fmt_money(s["revenue"]), "оплачено деньгами"),
        ("Прибыль", _fmt_money(s["revenue"] - cogs), f"себестоимость {_fmt_money(cogs)}"),
        ("Средний чек", _fmt_money(s["revenue"] // paid_n) if paid_n else "—",
         f"оплачено заказов: {paid_n}"),
        ("Заказов", str(s["orders"]),
         f"отменено {s['cancelled']}" if s["cancelled"] else "за период"),
        ("Не оплачено", _fmt_money(s["open_sum"]), f"в работе: {s['open_n']} шт"),
        ("Брони", str(s["rsv_total"]), f"аренда оплачено {_fmt_money(rent)}" if rent
         else "создано за период"),
    ])
    ax = _ch_axes(fig, [0.09, bottom - 0.36, 0.87, 0.27], f"Выручка {by}")
    _ch_bars(ax, labels, vals, _CH_COLORS[0])
    ax2 = _ch_axes(fig, [0.33, 0.06, 0.22, bottom - 0.53], "Топ позиций, шт")
    _ch_hbars(ax2, [_plain(r["name"], 26) for r in s["top"]],
              [int(r["q"]) for r in s["top"]], _CH_COLORS[1], lambda v: f"{int(v)}")
    ax3 = fig.add_axes([0.62, 0.10, 0.34, bottom - 0.55], facecolor=_CH_BG)
    ax3.set_title("Способы оплаты", color=_CH_FG, fontsize=13.5, fontweight="bold",
                  loc="left", pad=10)
    names = {"cash": "Наличные", "card": "Безнал", "transfer": "Перевод"}
    pay: dict[str, int] = {}
    for r in s["pay"]:
        k = names.get(r["m"] or "", "Не указан")
        pay[k] = pay.get(k, 0) + int(r["s"] or 0)
    _ch_donut(ax3, list(pay), list(pay.values()), _money_short(sum(pay.values())),
              "заказы, ₽")
    return _ch_png(fig)


def _card_segment(p: dict | None, today) -> str:
    """Главный сегмент гостя с картой (для кольцевой диаграммы)."""
    if not p or not p["all_days"]:
        return "Без оплат"
    days = p["all_days"]
    if (today - max(days)).days > SEG_SLEEP_DAYS:
        return "Давно не были"
    if p["all_m"] >= SEG_VIP_SPENT or len(days) >= SEG_VIP_VISITS:
        return "Ценные"
    if sum(1 for d in days if (today - d).days < 30) >= SEG_OFTEN_30:
        return "Ходят часто"
    if len(days) >= 3:
        return "Постоянные"
    return "Новые" if len(days) == 1 else "Пришли 2-й раз"


_SEG_ORDER = ("Новые", "Пришли 2-й раз", "Постоянные", "Ходят часто", "Ценные",
              "Давно не были", "Без оплат")


def loyalty_chart_png(o: dict) -> bytes:
    fig = _ch_new_fig()
    title = "всё время" if not o["days"] else f"{o['days']} дн."
    span = f"{o['since']:%d.%m} — {o['today']:%d.%m}" if o["since"] else title
    _ch_title(fig, "Zig Zag · Лояльность", span)
    avg_card = o["rev_card"] // o["n_card"] if o["n_card"] else 0
    n_other = o["n_all"] - o["n_card"]
    avg_other = (o["rev_all"] - o["rev_card"]) // n_other if n_other else 0
    bottom = _ch_tiles(fig, [
        ("Карт всего", str(o["cards"]), f"подтверждено {o['verified']}"),
        ("Новых карт", str(o["new_cards"]), f"за {title}"),
        ("Платили с картой", str(o["active"]),
         f"гостей · {_pct(o['rev_card'], o['rev_all'])}% выручки заказов"),
        ("Вернулись 2+ раз", f"{_pct(o['repeat'], o['active'])}%",
         f"{o['repeat']} из {o['active']}"),
        ("Баллы начислено", _fmt_pts(o["pts_earned"]), f"списано {_fmt_pts(o['pts_spent'])}"),
        ("Баллы на счетах", _fmt_pts(o["outstanding"]), "= ₽ будущих скидок"),
    ])
    # новые карты по времени
    days = [d for d in o.get("card_days", []) if not o["since"] or d >= o["since"]]
    if o["days"] and o["days"] <= 31:
        rng = [o["since"] + timedelta(days=i) for i in range(o["days"])]
        labels = [f"{d:%d.%m}" for d in rng]
        vals = [sum(1 for x in days if x == d) for d in rng]
        by = "по дням"
    elif o["days"]:
        start = o["since"] - timedelta(days=o["since"].weekday())
        labels, vals = [], []
        while start <= o["today"]:
            labels.append(f"{max(start, o['since']):%d.%m}")
            vals.append(sum(1 for x in days if start <= x <= start + timedelta(days=6)))
            start += timedelta(days=7)
        by = "по неделям"
    else:
        months = sorted({f"{d:%Y-%m}" for d in days})[-12:] or [f"{o['today']:%Y-%m}"]
        labels = [f"{_STATS_MONTHS[int(k[5:7]) - 1][:3]} {k[2:4]}" for k in months]
        vals = [sum(1 for x in days if f"{x:%Y-%m}" == k) for k in months]
        by = "по месяцам"
    ax = _ch_axes(fig, [0.08, bottom - 0.33, 0.50, 0.25], f"Новые карты {by}")
    _ch_bars(ax, labels, vals, _CH_COLORS[1], money=False)
    if any(vals):
        ax.yaxis.get_major_locator().set_params(integer=True)
    # средний чек с картой / без
    ax2 = _ch_axes(fig, [0.66, bottom - 0.33, 0.30, 0.25], "Средний чек, ₽")
    if o["n_card"] or n_other:
        _ch_bars(ax2, ["с картой", "без карты"], [avg_card, avg_other], _CH_COLORS[0])
        if len(ax2.patches) > 1:
            ax2.patches[1].set_facecolor(_CH_COLORS[7])
    else:
        _ch_empty(ax2)
    # топ гостей
    ax3 = _ch_axes(fig, [0.25, 0.06, 0.27, bottom - 0.47], "Топ гостей, ₽")
    _ch_hbars(ax3, [_plain(o["names"][u].get("first_name") or "Гость", 16) + f" ({n})"
                    for u, _, n in o["top"]],
              [m for _, m, _ in o["top"]], _CH_COLORS[2], _money_short)
    # сегменты карт
    seg: dict[str, int] = {}
    for u in o["names"]:
        k = _card_segment(o.get("per", {}).get(u), o["today"])
        seg[k] = seg.get(k, 0) + 1
    ax4 = fig.add_axes([0.60, 0.10, 0.36, bottom - 0.50], facecolor=_CH_BG)
    ax4.set_title("Кто держит карты", color=_CH_FG, fontsize=13.5, fontweight="bold",
                  loc="left", pad=10)
    keys = [k for k in _SEG_ORDER if seg.get(k)]
    _ch_donut(ax4, keys, [seg[k] for k in keys], str(o["cards"]), "карт")
    return _ch_png(fig)


async def _render_chart(fn, *args) -> bytes | None:
    """PNG или None (графики выключены / ошибка рисования — не мешаем тексту)."""
    if not _mpl():
        return None
    try:
        async with _CHART_LOCK:
            return await asyncio.to_thread(fn, *args)
    except Exception:
        logger.exception("Не удалось нарисовать график %s", getattr(fn, "__name__", fn))
        return None


def _remember_chart(chat_id: int, text_id: int, photo_id: int) -> None:
    _CHART_MSGS[(chat_id, text_id)] = photo_id
    while len(_CHART_MSGS) > 300:          # не копим бесконечно
        _CHART_MSGS.pop(next(iter(_CHART_MSGS)))


async def _answer_chart_and_text(message: Message, png: bytes | None, caption: str,
                                 text: str, kb) -> None:
    """Сначала картинка, под ней — текст с кнопками (кнопки остаются внизу)."""
    photo_id = None
    if png:
        try:
            pm = await message.answer_photo(BufferedInputFile(png, "zigzag.png"),
                                            caption=caption)
            photo_id = pm.message_id
        except Exception:
            logger.exception("Не удалось отправить график")
    tm = await message.answer(text, reply_markup=kb)
    if photo_id and tm:
        _remember_chart(tm.chat.id, tm.message_id, photo_id)


async def _update_chart(bot: Bot, chat_id: int, text_id: int, png: bytes | None,
                        caption: str) -> None:
    """Заменить картинку над текстом; если её нет (перезапуск) — прислать новую."""
    if not png:
        return
    pid = _CHART_MSGS.get((chat_id, text_id))
    if pid:
        try:
            await bot.edit_message_media(
                media=InputMediaPhoto(media=BufferedInputFile(png, "zigzag.png"),
                                      caption=caption),
                chat_id=chat_id, message_id=pid)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
        except Exception:
            logger.exception("Не удалось обновить график")
    try:
        pm = await bot.send_photo(chat_id, BufferedInputFile(png, "zigzag.png"),
                                  caption=caption)
        _remember_chart(chat_id, text_id, pm.message_id)
    except Exception:
        logger.exception("Не удалось отправить график")


async def _drop_chart(bot: Bot, chat_id: int, text_id: int) -> None:
    pid = _CHART_MSGS.pop((chat_id, text_id), None)
    if pid:
        try:
            await bot.delete_message(chat_id, pid)
        except Exception:
            pass


# ------------------------------------------- админ-команды -------------------
_STATS_MONTHS = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)


def _ddmm(date_s: str) -> str:
    return f"{date_s[8:10]}.{date_s[5:7]}"


def db_stats_range(d_from: str, d_to: str) -> dict:
    """Сводка по заказам, оплатам, продажам и броням за произвольный период."""
    with _connect() as conn:
        # выручка — только ОПЛАЧЕННЫЕ заказы и только деньгами (без баллов)
        o = conn.execute(
            "SELECT COUNT(*) AS n,"
            " COALESCE(SUM(CASE WHEN status = 'paid' THEN total - bonus_used END), 0) AS revenue,"
            " COALESCE(SUM(CASE WHEN status = 'paid' THEN bonus_used END), 0) AS bonus_used,"
            " COALESCE(SUM(CASE WHEN status IN ('accepted', 'ready', 'issued')"
            "   THEN total END), 0) AS open_sum,"
            " COALESCE(SUM(CASE WHEN status IN ('accepted', 'ready', 'issued')"
            "   THEN 1 ELSE 0 END), 0) AS open_n,"
            " COALESCE(SUM(CASE WHEN status = 'paid' AND kind = 'promo' THEN 1 ELSE 0 END), 0)"
            "   AS promo_n,"
            " COALESCE(SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END), 0) AS cancelled,"
            " COALESCE(SUM(CASE WHEN kind = 'pre' THEN 1 ELSE 0 END), 0) AS pre_n,"
            " COALESCE(SUM(CASE WHEN kind = 'pre' AND status = 'cancelled' THEN 1 ELSE 0 END), 0)"
            "   AS pre_cancel"
            " FROM orders WHERE substr(created_at, 1, 10) BETWEEN ? AND ?",
            (d_from, d_to),
        ).fetchone()
        accrued = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) AS a FROM bonus_moves"
            " WHERE kind IN ('accrual', 'offline') AND substr(created_at, 1, 10) BETWEEN ? AND ?",
            (d_from, d_to),
        ).fetchone()["a"]
        by_status = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM orders"
                " WHERE substr(created_at, 1, 10) BETWEEN ? AND ? GROUP BY status",
                (d_from, d_to),
            )
        }
        pay = [
            dict(r)
            for r in conn.execute(
                "SELECT COALESCE(pay_method, '') AS m, COUNT(*) AS n,"
                " COALESCE(SUM(total - bonus_used), 0) AS s FROM orders"
                " WHERE status = 'paid' AND substr(created_at, 1, 10) BETWEEN ? AND ?"
                " GROUP BY pay_method",
                (d_from, d_to),
            )
        ]
        top = [
            dict(r)
            for r in conn.execute(
                "SELECT oi.name AS name, SUM(oi.qty) AS q FROM order_items oi"
                " JOIN orders o ON o.id = oi.order_id"
                " WHERE o.status = 'paid'"
                " AND substr(o.created_at, 1, 10) BETWEEN ? AND ?"
                " GROUP BY oi.name ORDER BY q DESC, oi.name LIMIT 7",
                (d_from, d_to),
            )
        ]
        rsv = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM reservations"
                " WHERE substr(created_at, 1, 10) BETWEEN ? AND ? GROUP BY status",
                (d_from, d_to),
            )
        }
        rsv_pay = [
            dict(r)
            for r in conn.execute(
                "SELECT COALESCE(pay_method, '') AS m, COUNT(*) AS n,"
                " COALESCE(SUM(price), 0) AS s FROM reservations"
                " WHERE status = 'paid' AND substr(created_at, 1, 10) BETWEEN ? AND ?"
                " GROUP BY pay_method",
                (d_from, d_to),
            )
        ]
    return {
        "orders": o["n"],
        "revenue": o["revenue"],
        "bonus_used": o["bonus_used"],
        "bonus_accrued": int(accrued or 0),
        "open_sum": o["open_sum"],
        "open_n": o["open_n"],
        "promo_n": o["promo_n"],
        "cancelled": o["cancelled"],
        "pre_n": o["pre_n"],
        "pre_cancel": o["pre_cancel"],
        "by_status": by_status,
        "pay": pay,
        "top": top,
        "rsv": rsv,
        "rsv_total": sum(rsv.values()),
        "rsv_pay": rsv_pay,
    }


def stats_text(d_from: str, d_to: str) -> str:
    s = db_stats_range(d_from, d_to)
    cogs = db_cogs_range(d_from, d_to)
    period = _ddmm(d_from) if d_from == d_to else f"{_ddmm(d_from)} — {_ddmm(d_to)}"
    lines = [f"📊 <b>Аналитика</b> · {period}", ""]

    lines.append(
        f"📦 Заказов: <b>{s['orders']}</b>"
        + (f" · отменено {s['cancelled']}" if s["cancelled"] else "")
    )
    st_parts = [
        f"{STATUSES[c]}: {n}"
        for c, n in s["by_status"].items()
        if c in STATUSES and n
    ]
    if st_parts:
        lines.append("Статусы: " + " · ".join(st_parts))
    lines += [
        f"💰 Выручка (оплачено деньгами): <b>{_fmt_money(s['revenue'])}</b>",
        f"  себестоимость: {_fmt_money(cogs)}",
        f"  прибыль: <b>{_fmt_money(s['revenue'] - cogs)}</b>",
        f"  вложение на складе (сейчас): {_fmt_money(db_stock_value())}",
    ]
    if s["open_n"]:
        lines.append(f"⏳ Не оплачено (в работе): {s['open_n']} шт · {_fmt_money(s['open_sum'])}")
    if s["promo_n"]:
        lines.append(f"🔥 Оплачено заказов по акции: {s['promo_n']}")
    if s.get("pre_n"):
        lines.append(f"📝 Предзаказов: {s['pre_n']}"
                     + (f" · отменено {s['pre_cancel']}" if s.get("pre_cancel") else ""))
    if s["bonus_used"] or s["bonus_accrued"]:
        lines.append(
            f"💎 Баллы: списано в оплату {_fmt_pts(s['bonus_used'])}"
            f" · начислено {_fmt_pts(s['bonus_accrued'])}"
        )
    lines.append("")

    lines.append("💵 <b>Оплачено по способам:</b>")
    labels = {"cash": "Нал", "card": "Безнал", "transfer": "Перевод"}
    if s["pay"]:
        total_n = total_s = 0
        for row in s["pay"]:
            label = labels.get(row["m"] or "", "Не указан")
            lines.append(f" • {label} — {row['n']} шт · {_fmt_money(row['s'])}")
            total_n += row["n"]
            total_s += row["s"]
        lines.append(f" Итого: <b>{total_n} шт · {_fmt_money(total_s)}</b>")
    else:
        lines.append(" — оплаченных заказов за период нет")
    for row in s.get("rsv_pay") or []:
        rlabel = labels.get(row["m"] or "", "Не указан")
        lines.append(
            f" • Аренда ({rlabel}) — {row['n']} шт · {_fmt_money(row['s'])}"
        )
    lines.append("")

    lines.append("🔥 <b>Топ позиций (оплачено):</b>")
    if s["top"]:
        for i, row in enumerate(s["top"], 1):
            lines.append(f" {i}. {esc(row['name'])} — {row['q']} шт")
    else:
        lines.append(" — продаж за период нет")
    lines.append("")

    rsv_parts = []
    if s["rsv"].get("confirmed"):
        rsv_parts.append(f"✅ {s['rsv']['confirmed']}")
    if s["rsv"].get("new"):
        rsv_parts.append(f"🟡 {s['rsv']['new']}")
    if s["rsv"].get("cancelled"):
        rsv_parts.append(f"❌ {s['rsv']['cancelled']}")
    if s["rsv"].get("no_show"):
        rsv_parts.append(f"🚫 {s['rsv']['no_show']}")
    if s["rsv"].get("paid"):
        rsv_parts.append(f"💳 {s['rsv']['paid']}")
    rsv_line = f"📅 Броней создано: <b>{s['rsv_total']}</b>"
    if rsv_parts:
        rsv_line += " (" + " · ".join(rsv_parts) + ")"
    lines.append(rsv_line)
    return "\n".join(lines)


def kb_stats_period() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📅 Сегодня", "sg:day"), _btn("⏪ Вчера", "sg:yday")],
            [_btn("📊 Неделя", "sg:week"), _btn("📆 Месяц", "sg:month")],
            [_btn("🗓 Свой период", "sg:range")],
        ]
    )


def _range_prompt(mode: str, start: str | None = None) -> str:
    if mode == "end" and start:
        return (
            "🗓 <b>Период</b>\n\n"
            f"Начало: <b>{_ddmm(start)}</b> — выберите дату <b>ОКОНЧАНИЯ</b>:"
        )
    return "🗓 <b>Период</b>\n\nВыберите дату <b>НАЧАЛА</b>:"


def kb_month_grid(month: str, hi: str | None = None) -> InlineKeyboardMarkup:
    """Сетка дней месяца; будущие даты недоступны. month = YYYY-MM."""
    year, mon = int(month[:4]), int(month[5:7])
    today_iso = _now_dt().strftime("%Y-%m-%d")
    rows: list[list[InlineKeyboardButton]] = [
        [_btn(d, "noop") for d in ("пн", "вт", "ср", "чт", "пт", "сб", "вс")]
    ]
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, mon):
        row: list[InlineKeyboardButton] = []
        for day in week:
            if not day:
                row.append(_btn("·", "noop"))
                continue
            iso = f"{year:04d}-{mon:02d}-{day:02d}"
            if iso > today_iso:
                row.append(_btn("·", "noop"))
            elif iso == hi:
                row.append(_btn(f"✅{day}", f"sg:d:{iso}"))
            else:
                row.append(_btn(str(day), f"sg:d:{iso}"))
        rows.append(row)

    def _shift(m: str, delta: int) -> str:
        y, mm = int(m[:4]), int(m[5:7]) + delta
        y += (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        return f"{y:04d}-{mm:02d}"

    prev, nxt = _shift(month, -1), _shift(month, 1)
    rows.append(
        [
            _btn("◀️", f"sg:mv:{prev}") if prev >= "2024-01" else _btn(" ", "noop"),
            _btn(f"{_STATS_MONTHS[mon - 1]} {year}", "noop"),
            _btn("▶️", f"sg:mv:{nxt}") if nxt <= today_iso[:7] else _btn(" ", "noop"),
        ]
    )
    rows.append([_btn("🔙 Назад", "sg:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("stats"))
async def cmd_stats(message: Message, state: FSMContext):
    """CRM-аналитика: сегодня + кнопки периодов (день/неделя/месяц/свой)."""
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    today = _now_dt().strftime("%Y-%m-%d")
    png = await _render_chart(stats_chart_png, today, today)
    await _answer_chart_and_text(message, png, _stats_caption(today, today),
                                 stats_text(today, today), kb_stats_period())


def _stats_caption(d_from: str, d_to: str) -> str:
    period = _ddmm(d_from) if d_from == d_to else f"{_ddmm(d_from)} — {_ddmm(d_to)}"
    return f"📊 Статистика · {period}"


async def _stats_show(cb: CallbackQuery, d_from: str, d_to: str) -> None:
    """Обновить текст сводки и картинку над ним."""
    try:
        await cb.message.edit_text(stats_text(d_from, d_to), reply_markup=kb_stats_period())
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return  # тот же период ещё раз — всё уже на экране
        raise
    png = await _render_chart(stats_chart_png, d_from, d_to)
    await _update_chart(cb.bot, cb.message.chat.id, cb.message.message_id, png,
                        _stats_caption(d_from, d_to))


@router.callback_query(F.data.startswith("sg:"))
async def cb_stats(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("⛔ Только для администратора", show_alert=True)
        return
    parts = cb.data.split(":")
    today = _now_dt()
    today_iso = today.strftime("%Y-%m-%d")
    try:
        # --- быстрые периоды ---
        if parts[1] in ("day", "yday", "week", "month"):
            await state.clear()
            d_to = today_iso
            if parts[1] == "day":
                d_from = today_iso
            elif parts[1] == "yday":  # календарное вчера (как «Сегодня» — по датам)
                d_from = d_to = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            elif parts[1] == "week":
                d_from = (today - timedelta(days=6)).strftime("%Y-%m-%d")
            else:
                d_from = (today - timedelta(days=29)).strftime("%Y-%m-%d")
            await cb.answer()
            await _stats_show(cb, d_from, d_to)
            return
        # --- свой период: календарь ---
        if parts[1] == "range":
            await cb.answer()
            await state.set_state(StatsFSM.picking)
            await state.update_data(mode="start", ed_msg=cb.message.message_id)
            await cb.message.edit_text(
                _range_prompt("start"), reply_markup=kb_month_grid(today_iso[:7])
            )
            return
        if parts[1] == "back":
            await state.clear()
            await cb.answer()
            await _stats_show(cb, today_iso, today_iso)
            return
        if parts[1] == "mv":  # листание месяцев
            if await state.get_state() != StatsFSM.picking:
                await state.set_state(StatsFSM.picking)
                await state.update_data(mode="start", ed_msg=cb.message.message_id)
            data = await state.get_data()
            mode = data.get("mode") or "start"
            start = data.get("start")
            await cb.answer()
            await cb.message.edit_text(
                _range_prompt(mode, start),
                reply_markup=kb_month_grid(
                    parts[2], hi=start if mode == "end" else None
                ),
            )
            return
        if parts[1] == "d":  # клик по дню
            day = parts[2]
            if await state.get_state() != StatsFSM.picking:
                await state.set_state(StatsFSM.picking)
                await state.update_data(mode="start", ed_msg=cb.message.message_id)
            data = await state.get_data()
            mode = data.get("mode") or "start"
            if mode == "start":
                await state.update_data(mode="end", start=day)
                await cb.answer()
                await cb.message.edit_text(
                    _range_prompt("end", day),
                    reply_markup=kb_month_grid(day[:7], hi=day),
                )
                return
            start = data.get("start") or day
            d_from, d_to = sorted((start, day))
            await state.clear()
            await cb.answer("Готово ✅")
            await _stats_show(cb, d_from, d_to)
            return
    except Exception:
        logger.exception("Ошибка в колбэке статистики: %s", cb.data)
        try:
            await cb.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.message(Command("orders"))
async def cmd_orders(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    orders = db_recent_orders(10)
    if not orders:
        await message.answer("Заказов пока нет.")
        return
    lines = ["🗂 Последние заказы:", ""]
    for o in orders:
        status = STATUSES.get(o["status"], o["status"])
        lines.append(
            f"#{o['id']} · {_place_short(o['table_no'])} · {_fmt_money(o['total'])} · "
            f"{status} · {o['created_at'][11:16]}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("timing"))
async def cmd_timing(message: Message):
    """Статистика: сколько времени проходит от принятия заказа до выдачи."""
    if message.from_user.id not in ADMIN_IDS:
        return
    now = _now_dt()
    issued, active = db_timing_today()
    lines = ["⏱ <b>Сроки выдачи</b> (сегодня)", ""]
    if issued:
        durations: list[int] = []
        for row in issued:
            c = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
            i = datetime.strptime(row["issued_at"], "%Y-%m-%d %H:%M:%S")
            mins = max(0, int((i - c).total_seconds() // 60))
            durations.append(mins)
            lines.append(f"#{row['id']} · {_place_short(row['table_no'])} → выдан за <b>{mins} мин</b>")
        avg = round(sum(durations) / len(durations))
        lines += [
            "",
            f"Среднее время выдачи: <b>{avg} мин</b>",
            f"Выдано заказов: <b>{len(durations)}</b>",
        ]
    else:
        lines.append("Сегодня ничего ещё не выдано.")
    if active:
        lines += ["", "🟢 <b>В работе сейчас:</b>"]
        for row in active:
            c = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
            mins = max(0, int((now - c).total_seconds() // 60))
            st = STATUSES.get(row["status"], row["status"])
            lines.append(
                f"#{row['id']} · {_place_short(row['table_no'])} · {st} · в работе {mins} мин"
            )
    await message.answer("\n".join(lines))


# ------------------------------------- /stock и /recipe (админ) -------------
class StockFSM(StatesGroup):
    qty = State()      # приход/расход: количество
    money = State()    # приход: сумма закупки (расчёт средней с/с)
    cost = State()     # себестоимость за единицу
    minq = State()     # минимальный остаток
    new_name = State() # создание: название
    new_qty = State()  # создание: начальный остаток
    new_cost = State() # создание: себестоимость
    new_minq = State() # создание: мин. остаток


class RecipeFSM(StatesGroup):
    qty = State()      # сколько ингредиента на 1 порцию


class StatsFSM(StatesGroup):
    picking = State()  # выбор периода в /stats: mode=start|end, start, ed_msg


async def _admin_msg_guard(message: Message, state: FSMContext) -> bool:
    """Ввод в шагах склада/рецептов — только админам; чужой — сбрасываем."""
    if _is_admin(message.from_user.id if message.from_user else None):
        return True
    await state.clear()
    return False


def _parse_num(t: str) -> float | None:
    """«500» / «10,5» → число; мусор → None."""
    try:
        v = float(t.replace(",", ".").replace(" ", ""))
    except (ValueError, AttributeError):
        return None
    return v if v >= 0 else None


def _menu_item(item_id: str) -> dict | None:
    for c in MENU["categories"]:
        for it in c.get("items", []):
            if it.get("id") == item_id:
                return it
    return None


def _cancel_kb(cb_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("✖ Отмена", cb_data)]])


async def _edit_or_send(message: Message, data: dict, text: str, kb) -> None:
    """Правит сохранённое сообщение (data['ed_msg']); если не вышло — шлёт новое."""
    mid = data.get("ed_msg")
    if mid:
        try:
            await message.bot.edit_message_text(
                text=text, chat_id=message.chat.id, message_id=mid, reply_markup=kb
            )
            return
        except Exception:
            logger.info("Не удалось отредактировать сообщение %s, шлём новое", mid)
    try:
        await message.answer(text, reply_markup=kb)
    except Exception:
        logger.exception("Не удалось отправить сообщение")


def stock_list_text() -> str:
    ings = db_list_ingredients()
    head = (
        "📦 <b>Склад</b>\n"
        f"Вложение на складе: <b>{_fmt_money(db_stock_value())}</b>"
    )
    if not ings:
        return head + "\n\nПока пусто — добавьте первую позицию кнопкой ниже."
    lines = [head, ""]
    for ing in ings:
        mark = ""
        if float(ing["qty"]) <= 0:
            mark = " ⛔"
        elif float(ing["min_qty"]) > 0 and float(ing["qty"]) <= float(ing["min_qty"]):
            mark = " ⚠️"
        lines.append(
            f"• {esc(ing['name'])} — {_fmt_qty(ing['qty'])} {esc(ing['unit'])}{mark}"
        )
    return "\n".join(lines)


def stock_detail_text(ing: dict) -> str:
    mark = ""
    if float(ing["qty"]) <= 0:
        mark = " ⛔"
    elif float(ing["min_qty"]) > 0 and float(ing["qty"]) <= float(ing["min_qty"]):
        mark = " ⚠️"
    min_note = (
        f"{_fmt_qty(ing['min_qty'])} {esc(ing['unit'])}"
        if float(ing["min_qty"]) > 0
        else "не контролируется"
    )
    lines = [
        f"📦 <b>{esc(ing['name'])}</b>",
        "",
        f"Остаток: <b>{_fmt_qty(ing['qty'])} {esc(ing['unit'])}</b>{mark}",
        f"Себестоимость: {_fmt_price(ing['cost'])} за {esc(ing['unit'])}",
        f"Вложено: <b>{_fmt_money(round(float(ing['qty']) * float(ing['cost'])))}</b>",
        f"Мин. остаток: {min_note}",
    ]
    moves = db_recent_moves(ing["id"])
    if moves:
        lines += ["", "<b>Последние движения:</b>"]
        for m in moves:
            d = float(m["delta"])
            sign = "+" if d >= 0 else "−"
            when = (m.get("created_at") or "")[5:16]
            ref = f" · {m['ref']}" if m.get("ref") else ""
            lines.append(
                f"  {sign}{_fmt_qty(abs(d))} {esc(ing['unit'])} · {m['reason']}"
                f"{ref} · {when}"
            )
    return "\n".join(lines)


def kb_stock_list() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for ing in db_list_ingredients():
        rows.append(
            [
                _btn(
                    f"{ing['name']} · {_fmt_qty(ing['qty'])} {ing['unit']}",
                    f"sk:{ing['id']}",
                )
            ]
        )
    rows.append([_btn("➕ Новая позиция", "skn"), _btn("📋 Таблица", "sx:table")])
    rows.append([_btn("✖ Закрыть", "sk:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_stock_detail(ing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("➕ Приход", f"sk:{ing_id}:in"),
                _btn("➖ Расход", f"sk:{ing_id}:out"),
            ],
            [
                _btn("✏️ Себестоимость", f"sk:{ing_id}:cost"),
                _btn("📉 Мин.остаток", f"sk:{ing_id}:min"),
            ],
            [_btn("📏 Единица", f"sk:{ing_id}:unit")],
            [_btn("🗑 Удалить позицию", f"sk:{ing_id}:rm")],
            [_btn("🔙 Назад", "sk:list")],
        ]
    )


@router.message(Command("stock"))
async def cmd_stock(message: Message, state: FSMContext):
    """Склад: остатки, себестоимость, приход/расход."""
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await _send_stock_table(message)


@router.callback_query(F.data.startswith("sk:"))
async def cb_stock(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("⛔ Только для администратора", show_alert=True)
        return
    parts = cb.data.split(":")
    try:
        # --- приход введён, но сумму закупки не меняем ---
        if len(parts) == 2 and parts[1] == "costskip":
            data = await state.get_data()
            ing = db_get_ingredient(data.get("sk_ing") or 0)
            pending = float(data.get("sk_pending_qty") or 0)
            await state.clear()
            if not ing or pending <= 0:
                await cb.answer("Нечего приходовать", show_alert=True)
                return
            updated = stock_move(ing["id"], +pending, "приход")
            await cb.answer("Принято ✅ (с/с не менялась)")
            if updated:
                await _stock_alerts(cb.bot, [updated])
            try:
                await cb.message.edit_text(
                    f"✅ Приход: {_fmt_qty(pending)} {esc(updated['unit'])} "
                    f"(без изменения с/с)\n\n" + stock_detail_text(updated),
                    reply_markup=kb_stock_detail(ing["id"]),
                )
            except TelegramBadRequest:
                pass
            return
        # --- служебные ---
        if len(parts) == 2 and parts[1] in ("list", "close", "cancel"):
            await state.clear()
            await cb.answer("Отменили" if parts[1] == "cancel" else "Готово")
            text = stock_list_text() if parts[1] != "close" else "✅ Готово"
            kb = kb_stock_list() if parts[1] != "close" else None
            try:
                await cb.message.edit_text(text, reply_markup=kb)
            except TelegramBadRequest:
                pass
            return
        # --- смена единицы измерения: показать выбор ---
        if len(parts) == 3 and parts[2] == "unit":
            ing = db_get_ingredient(int(parts[1]))
            if not ing:
                await cb.answer("Позиция не найдена", show_alert=True)
                return
            await cb.answer()
            try:
                await cb.message.edit_text(
                    f"📏 <b>{esc(ing['name'])}</b>\n\n"
                    "Единица измерения для остатков и рецептов.\n"
                    "Число остатка не изменится — после смены поправьте его, "
                    "если нужно.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                _btn(u, f"sk:{ing['id']}:u:{u}")
                                for u in ("шт", "г", "мл")
                            ],
                            [_btn("🔙 Назад", f"sk:{ing['id']}")],
                        ]
                    ),
                )
            except TelegramBadRequest:
                pass
            return
        # --- карточка позиции ---
        if len(parts) == 2:
            ing = db_get_ingredient(int(parts[1]))
            if not ing:
                await cb.answer("Позиция не найдена", show_alert=True)
                return
            await cb.answer()
            try:
                await cb.message.edit_text(
                    stock_detail_text(ing), reply_markup=kb_stock_detail(ing["id"])
                )
            except TelegramBadRequest:
                pass
            return
        # --- приход/расход/себестоимость/минимум/удаление ---
        if len(parts) == 3:
            ing = db_get_ingredient(int(parts[1]))
            act = parts[2]
            if not ing:
                await cb.answer("Позиция не найдена", show_alert=True)
                return
            if act == "rm":  # экран подтверждения удаления
                await cb.answer()
                await cb.message.edit_text(
                    f"🗑 <b>Удалить позицию?</b>\n\n"
                    f"{esc(ing['name'])} — остаток "
                    f"{_fmt_qty(ing['qty'])} {esc(ing['unit'])}\n\n"
                    "Позиция исчезнет из склада и из всех рецептов. "
                    "Действие необратимо.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [_btn("🗑 Да, удалить", f"sk:{ing['id']}:yes")],
                            [_btn("🔙 Назад", f"sk:{ing['id']}")],
                        ]
                    ),
                )
                return
            if act == "yes":  # подтверждено — удаляем
                db_delete_ingredient(ing["id"])
                await cb.answer(f"«{ing['name']}» удалена")
                try:
                    await cb.message.edit_text(
                        stock_list_text(), reply_markup=kb_stock_list()
                    )
                except TelegramBadRequest:
                    pass
                return
            prompts = {
                "in": (
                    f"➕ Приход: <b>{esc(ing['name'])}</b>\n\n"
                    f"Сколько поступило ({esc(ing['unit'])})? "
                    "Отправьте числом, например 500"
                ),
                "out": (
                    f"➖ Расход: <b>{esc(ing['name'])}</b>\n\n"
                    f"Сколько списать ({esc(ing['unit'])})? "
                    f"На складе сейчас: {_fmt_qty(ing['qty'])}"
                ),
                "cost": (
                    f"✏️ Себестоимость: <b>{esc(ing['name'])}</b>\n\n"
                    f"Цена за 1 {esc(ing['unit'])} в ₽ (можно с копейками, например 6,5)"
                ),
                "min": (
                    f"📉 Мин. остаток: <b>{esc(ing['name'])}</b>\n\n"
                    f"Порог ({esc(ing['unit'])}), 0 — не контролировать"
                ),
            }
            if act not in prompts:
                await cb.answer()
                return
            states = {
                "in": StockFSM.qty,
                "out": StockFSM.qty,
                "cost": StockFSM.cost,
                "min": StockFSM.minq,
            }
            await cb.answer()
            await state.set_state(states[act])
            await state.update_data(
                sk_ing=ing["id"], sk_action=act, ed_msg=cb.message.message_id
            )
            await cb.message.edit_text(
                prompts[act], reply_markup=_cancel_kb("sk:cancel")
            )
            return
        # --- применение новой единицы ---
        if len(parts) == 4 and parts[2] == "u":
            ing = db_get_ingredient(int(parts[1]))
            if not ing:
                await cb.answer("Позиция не найдена", show_alert=True)
                return
            if parts[3] not in ("шт", "г", "мл"):
                await cb.answer("Неизвестная единица", show_alert=True)
                return
            db_set_stock_unit(ing["id"], parts[3])
            updated = db_get_ingredient(ing["id"])
            await cb.answer(f"Единица: {parts[3]}")
            try:
                await cb.message.edit_text(
                    stock_detail_text(updated),
                    reply_markup=kb_stock_detail(updated["id"]),
                )
            except TelegramBadRequest:
                pass
            return
    except Exception:
        logger.exception("Ошибка в колбэке склада: %s", cb.data)
        try:
            await cb.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("skn"))
async def cb_stock_new(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("⛔ Только для администратора", show_alert=True)
        return
    parts = cb.data.split(":")
    try:
        if cb.data == "skn":
            await cb.answer()
            await state.set_state(StockFSM.new_name)
            await state.update_data(ed_msg=cb.message.message_id)
            await cb.message.edit_text(
                "➕ <b>Новая позиция склада</b>\n\n"
                "Введите название (например «Табак Musthave»):",
                reply_markup=_cancel_kb("skn:cancel"),
            )
            return
        if cb.data == "skn:cancel":
            await state.clear()
            await cb.answer("Отменили")
            try:
                await cb.message.edit_text(
                    stock_list_text(), reply_markup=kb_stock_list()
                )
            except TelegramBadRequest:
                pass
            return
        if len(parts) == 3 and parts[1] == "unit":
            data = await state.get_data()
            if "sk_new_name" not in data:
                await cb.answer("Начните заново через /stock", show_alert=True)
                return
            await cb.answer()
            await state.update_data(unit=parts[2])
            await state.set_state(StockFSM.new_qty)
            await cb.message.edit_text(
                f"«{esc(data['sk_new_name'])}», единица: <b>{esc(parts[2])}</b>\n\n"
                "Введите начальный остаток (число, можно 0):",
                reply_markup=_cancel_kb("skn:cancel"),
            )
            return
    except Exception:
        logger.exception("Ошибка в колбэке новой позиции: %s", cb.data)
        try:
            await cb.answer("Ошибка", show_alert=True)
        except Exception:
            pass


# --- ввод чисел для склада ---
@router.message(StockFSM.qty, F.text)
async def stock_qty_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    data = await state.get_data()
    ing = db_get_ingredient(data.get("sk_ing") or 0)
    v = _parse_num((message.text or "").strip())
    if ing is None:
        await state.clear()
        await message.answer("Позиция не найдена. Откройте /stock заново.")
        return
    if v is None or v <= 0 or v > 1_000_000:
        await message.answer(
            "Введите число больше 0 (например 500), либо ✖ Отмена кнопкой выше."
        )
        return
    action = data.get("sk_action")
    if action != "out":
        # приход: сперва сумма закупки — по ней посчитаем среднюю себестоимость
        await state.set_state(StockFSM.money)
        await state.update_data(sk_pending_qty=v)
        await _edit_or_send(
            message,
            data,
            f"➕ Принято: {_fmt_qty(v)} {esc(ing['unit'])} «{esc(ing['name'])}».\n\n"
            "Сколько заплатили за это поступление, ₽ — сумма за всё сразу?\n"
            "Например: 10 упаковок × 650 ₽ = 6500\n"
            "(себестоимость посчитается автоматически)",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [_btn("⏭ Не менять с/с", "sk:costskip")],
                    [_btn("✖ Отмена", "sk:cancel")],
                ]
            ),
        )
        return
    if v > float(ing["qty"]):
        await message.answer(
            f"Недостаточно: на складе {_fmt_qty(ing['qty'])} {ing['unit']}. "
            "Введите число не больше этого."
        )
        return
    updated = stock_move(ing["id"], -v, "расход")
    if updated is None:
        await state.clear()
        await message.answer("Позиция не найдена.")
        return
    await state.clear()
    await _stock_alerts(message.bot, [updated])
    await _edit_or_send(
        message,
        data,
        f"✅ Списано: {_fmt_qty(v)} {esc(updated['unit'])} → "
        f"остаток {_fmt_qty(updated['qty'])} {esc(updated['unit'])}\n\n"
        + stock_detail_text(updated),
        kb_stock_detail(ing["id"]),
    )


@router.message(StockFSM.money, F.text)
async def stock_money_input(message: Message, state: FSMContext):
    """Сумма закупки прихода → средневзвешенная себестоимость."""
    if not await _admin_msg_guard(message, state):
        return
    data = await state.get_data()
    ing = db_get_ingredient(data.get("sk_ing") or 0)
    pending = float(data.get("sk_pending_qty") or 0)
    v = _parse_num((message.text or "").strip())
    if ing is None:
        await state.clear()
        await message.answer("Позиция не найдена. Откройте /stock заново.")
        return
    if v is None or v < 0 or v > 100_000_000 or pending <= 0:
        await message.answer(
            "Введите сумму в ₽ числом (например 6500), либо ✖ Отмена."
        )
        return
    # средневзвешенная: (старый остаток × старая с/с + сумма закупки) / новый остаток
    old_q, old_c = float(ing["qty"]), float(ing["cost"])
    if old_q + pending > 0:
        new_cost = (old_q * old_c + v) / (old_q + pending)
    else:
        new_cost = old_c
    db_set_cost(ing["id"], new_cost)
    updated = stock_move(ing["id"], +pending, "приход", ref=_fmt_price(v))
    await state.clear()
    if updated is None:
        await message.answer("Позиция не найдена.")
        return
    await _stock_alerts(message.bot, [updated])
    await _edit_or_send(
        message,
        data,
        f"✅ Приход: {_fmt_qty(pending)} {esc(updated['unit'])} за {_fmt_price(v)}\n"
        f"Себестоимость: {_fmt_price(new_cost)} за {esc(updated['unit'])}\n\n"
        + stock_detail_text(updated),
        kb_stock_detail(ing["id"]),
    )


@router.message(StockFSM.cost, F.text)
async def stock_cost_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    data = await state.get_data()
    ing = db_get_ingredient(data.get("sk_ing") or 0)
    v = _parse_num((message.text or "").strip())
    if ing is None:
        await state.clear()
        await message.answer("Позиция не найдена. Откройте /stock заново.")
        return
    if v is None or v < 0 or v > 10_000_000:
        await message.answer("Введите цену в ₽ числом (можно с копейками), либо ✖ Отмена.")
        return
    db_set_cost(ing["id"], v)
    await state.clear()
    updated = db_get_ingredient(ing["id"])
    await _edit_or_send(
        message,
        data,
        f"✅ Себестоимость обновлена\n\n{stock_detail_text(updated)}",
        kb_stock_detail(ing["id"]),
    )


@router.message(StockFSM.minq, F.text)
async def stock_min_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    data = await state.get_data()
    ing = db_get_ingredient(data.get("sk_ing") or 0)
    v = _parse_num((message.text or "").strip())
    if ing is None:
        await state.clear()
        await message.answer("Позиция не найдена. Откройте /stock заново.")
        return
    if v is None or v < 0 or v > 10_000_000:
        await message.answer("Введите число (можно 0), либо ✖ Отмена.")
        return
    db_set_min_qty(ing["id"], v)
    await state.clear()
    updated = db_get_ingredient(ing["id"])
    await _edit_or_send(
        message,
        data,
        f"✅ Минимальный остаток обновлён\n\n{stock_detail_text(updated)}",
        kb_stock_detail(ing["id"]),
    )


# --- создание новой позиции ---
@router.message(StockFSM.new_name, F.text)
async def stock_new_name_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    t = (message.text or "").strip()
    if not t or len(t) > 60:
        await message.answer("Введите название до 60 символов сообщением ✍️")
        return
    if any(i["name"].lower() == t.lower() for i in db_list_ingredients()):
        await message.answer("Позиция с таким названием уже есть. Введите другое.")
        return
    await state.update_data(sk_new_name=t)
    data = await state.get_data()
    await _edit_or_send(
        message,
        data,
        f"«{esc(t)}» — единица измерения:",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _btn("шт", "skn:unit:шт"),
                    _btn("г", "skn:unit:г"),
                    _btn("мл", "skn:unit:мл"),
                ],
                [_btn("✖ Отмена", "skn:cancel")],
            ]
        ),
    )


@router.message(StockFSM.new_qty, F.text)
async def stock_new_qty_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    v = _parse_num((message.text or "").strip())
    if v is None or v < 0 or v > 10_000_000:
        await message.answer("Введите число (можно 0), либо ✖ Отмена.")
        return
    await state.update_data(sk_new_qty=v)
    await state.set_state(StockFSM.new_cost)
    data = await state.get_data()
    await _edit_or_send(
        message,
        data,
        "Себестоимость за 1 единицу, ₽ (можно с копейками, например 6,5):",
        _cancel_kb("skn:cancel"),
    )


@router.message(StockFSM.new_cost, F.text)
async def stock_new_cost_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    v = _parse_num((message.text or "").strip())
    if v is None or v < 0 or v > 10_000_000:
        await message.answer("Введите цену в ₽ числом (можно 0), либо ✖ Отмена.")
        return
    await state.update_data(sk_new_cost=float(v))
    await state.set_state(StockFSM.new_minq)
    data = await state.get_data()
    await _edit_or_send(
        message,
        data,
        "Минимальный остаток для контроля (число, 0 — не контролировать):",
        _cancel_kb("skn:cancel"),
    )


@router.message(StockFSM.new_minq, F.text)
async def stock_new_minq_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    data = await state.get_data()
    v = _parse_num((message.text or "").strip())
    if v is None or v < 0 or v > 10_000_000:
        await message.answer("Введите число (можно 0), либо ✖ Отмена.")
        return
    try:
        ing_id = db_add_ingredient(
            data["sk_new_name"],
            data.get("unit", "шт"),
            data.get("sk_new_qty", 0),
            data.get("sk_new_cost", 0),
            v,
        )
    except Exception:
        logger.exception("Не удалось создать позицию склада")
        await state.clear()
        await message.answer("Не удалось создать позицию. Попробуйте через /stock.")
        return
    await state.clear()
    ing = db_get_ingredient(ing_id)
    await _edit_or_send(
        message,
        data,
        f"✅ Позиция создана\n\n{stock_detail_text(ing)}",
        kb_stock_detail(ing_id),
    )


# ------------------------------- /stock: таблица ------------------------------
# Склад одним экраном + массовые правки: текстом (строка на позицию) или
# Excel-файлом (скачать → заполнить жёлтые колонки → прислать обратно).
# Оба пути: разбор → предпросмотр «было → стало» → ✅ Применить (атомарно,
# с записью в журнал движений) → алерты «мало/закончилось».
class StockBulkFSM(StatesGroup):
    text = State()     # ждём сообщение с правками
    confirm = State()  # показан предпросмотр: sx_ops, sx_src


SX_MAX_OPS = 300                 # строк правок за один раз
SX_MAX_FILE = 2 * 1024 * 1024    # размер Excel-файла, байт
SX_MAX_QTY = 1_000_000
SX_MAX_SUM = 100_000_000
SX_MAX_COST = 10_000_000
SX_MARK = "zigzag-stock-v1"
_SX_NUM = r"\d+(?:[.,]\d+)?"


def _q(v: float) -> str:
    """Количество без «1e+06» и хвостовых нулей: 10.500 → «10.5»."""
    t = f"{float(v):.3f}".rstrip("0").rstrip(".")
    return "0" if t in ("-0", "") else t


def _stock_flag(ing: dict) -> str:
    q, mn = float(ing["qty"]), float(ing["min_qty"])
    if q <= 0:
        return " ⛔"
    if mn > 0 and q <= mn:
        return " ⚠️"
    return ""


def stock_table_texts() -> list[str]:
    """Таблица склада моноширинным шрифтом; длинная — несколькими сообщениями."""
    ings = db_list_ingredients()
    out_n = sum(1 for i in ings if float(i["qty"]) <= 0)
    low_n = sum(1 for i in ings if float(i["qty"]) > 0 and float(i["min_qty"]) > 0
                and float(i["qty"]) <= float(i["min_qty"]))
    head = [f"📦 <b>Склад</b> · вложено <b>{_fmt_money(db_stock_value())}</b>"]
    if out_n or low_n:
        head.append(f"⛔ закончилось: {out_n} · ⚠️ мало: {low_n}")
    if not ings:
        return ["\n".join(head) + "\n\nПока пусто — добавьте первую позицию кнопкой ниже."]
    # ~36 символов в строке — помещается на экран телефона без переноса
    rows = [f"{ing['id']:>3} {_plain(ing['name'], 19):<19} "
            f"{_q(ing['qty']) + _plain(ing['unit'], 3):>7} "
            f"{_q(ing['min_qty']) if float(ing['min_qty']) > 0 else '—':>4}"
            f"{_stock_flag(ing)}" for ing in ings]
    hdr = f"{'№':>3} {'Позиция':<19} {'Ост.':>7} {'Мин':>4}"
    foot = ("\n\n№ — номер для правок текстом. С/с и суммы — в Excel или "
            "«🗂 По позициям».")
    chunks: list[list[str]] = [[]]
    size = 0
    for r in rows:
        if size + len(r) > 3300 and chunks[-1]:
            chunks.append([])
            size = 0
        chunks[-1].append(esc(r))
        size += len(r) + 1
    texts = []
    for i, ch in enumerate(chunks):
        t = ("\n".join(head) + "\n\n" if i == 0 else f"📦 Склад · продолжение {i + 1}\n\n")
        t += f"<pre>{esc(hdr)}\n" + "\n".join(ch) + "</pre>"
        if i == len(chunks) - 1:
            t += foot
        texts.append(t)
    return texts


def kb_stock_table() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📥 Скачать Excel", "sx:xlsx"), _btn("✏️ Правки текстом", "sx:bulk")],
        [_btn("🗂 По позициям", "sk:list"), _btn("➕ Новая позиция", "skn")],
        [_btn("✖ Закрыть", "sk:close")],
    ])


def _sx_help(example_id: int | None = None) -> str:
    n = example_id or 12
    return (
        "✏️ <b>Массовые правки склада</b>\n\n"
        "Пришлите одним сообщением, по строке на изменение. "
        "Первое число — № из таблицы /stock:\n"
        f"<code>{n} +5</code> — приход 5\n"
        f"<code>{n} +5 3250</code> — приход 5 на сумму 3250 ₽ (с/с пересчитается)\n"
        f"<code>{n} -2</code> — расход (списание) 2\n"
        f"<code>{n} =40</code> — остаток стал 40 (инвентаризация)\n"
        f"<code>{n} =40 650</code> — остаток 40 и с/с 650 ₽ за ед.\n"
        f"<code>{n} сс 650</code> — себестоимость 650 ₽ за ед.\n"
        f"<code>{n} мин 10</code> — минимальный остаток 10 (0 — не следить)\n\n"
        "Числа без пробелов внутри, дробные — через точку или запятую. "
        "Сначала покажу, что изменится, и только после ✅ применю."
    )


def _sx_num(t: str) -> float:
    return float(t.replace(",", "."))


def sx_parse_text(text: str) -> tuple[list[dict], list[str]]:
    """Строки «№ операция» → (операции, ошибки)."""
    ops: list[dict] = []
    errs: list[str] = []
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) > SX_MAX_OPS:
        return [], [f"Слишком много строк ({len(lines)}), максимум {SX_MAX_OPS} за раз."]
    N = _SX_NUM
    for k, ln in enumerate(lines, 1):
        t = ln.lower().replace("−", "-").replace("–", "-").replace("—", "-")
        t = re.sub(r"\s*(₽|руб\.?|р\.?)\s*$", "", t)
        m = re.fullmatch(r"#?(\d{1,7})[.):]?\s+(.+)", t)
        if not m:
            errs.append(f"стр. {k} «{esc(ln[:40])}»: начните с № позиции")
            continue
        iid, rest = int(m.group(1)), m.group(2).strip()
        op = None
        if mm := re.fullmatch(rf"\+\s*({N})(?:\s+(?:за\s+)?({N}))?", rest):
            op = {"op": "in", "v": _sx_num(mm.group(1)),
                  "sum": _sx_num(mm.group(2)) if mm.group(2) else None}
        elif mm := re.fullmatch(rf"-\s*({N})", rest):
            op = {"op": "out", "v": _sx_num(mm.group(1))}
        elif mm := re.fullmatch(rf"=\s*({N})(?:\s+(?:сс\s*|с/с\s*)?({N}))?", rest):
            op = {"op": "set", "v": _sx_num(mm.group(1)),
                  "cost": _sx_num(mm.group(2)) if mm.group(2) else None}
        elif mm := re.fullmatch(rf"(?:сс|с/с|cost)\s*=?\s*({N})", rest):
            op = {"op": "cost", "v": _sx_num(mm.group(1))}
        elif mm := re.fullmatch(rf"(?:мин|min)\.?\s*=?\s*({N})", rest):
            op = {"op": "min", "v": _sx_num(mm.group(1))}
        if op is None:
            errs.append(f"стр. {k} «{esc(ln[:40])}»: не понял операцию")
            continue
        op["id"] = iid
        op["src"] = f"стр. {k}"
        ops.append(op)
    return ops, errs


def _sx_check_bounds(op: dict) -> str | None:
    v = op["v"]
    if op["op"] in ("in", "out") and v <= 0:
        return "количество должно быть больше 0"
    if op["op"] in ("in", "out", "set", "min") and v > SX_MAX_QTY:
        return "слишком большое количество"
    if op["op"] == "cost" and v > SX_MAX_COST:
        return "слишком большая себестоимость"
    if op.get("sum") is not None and op["sum"] > SX_MAX_SUM:
        return "слишком большая сумма закупки"
    if op.get("cost") is not None and op["cost"] > SX_MAX_COST:
        return "слишком большая себестоимость"
    return None


def _sx_step(cur: dict, op: dict) -> str | None:
    """Применяет операцию к словарю {qty, cost, min_qty}. Ошибка → текст."""
    q, c = float(cur["qty"]), float(cur["cost"])
    v = float(op["v"])
    if op["op"] == "in":
        if op.get("sum") is not None:
            # средневзвешенная, как при ручном приходе в карточке позиции
            cur["cost"] = (q * c + float(op["sum"])) / (q + v) if q + v > 0 else c
        cur["qty"] = q + v
    elif op["op"] == "out":
        if v > q + 1e-9:
            return f"списать {_q(v)}, а на складе {_q(q)}"
        cur["qty"] = q - v
    elif op["op"] == "set":
        cur["qty"] = v
        if op.get("cost") is not None:
            cur["cost"] = float(op["cost"])
    elif op["op"] == "cost":
        cur["cost"] = v
    elif op["op"] == "min":
        cur["min_qty"] = v
    return None


def sx_preview(ops: list[dict], errs: list[str], src: str) -> tuple[str, list[dict]]:
    """Текст предпросмотра и список операций, которые пройдут проверку."""
    ings = {int(i["id"]): i for i in db_list_ingredients()}
    errs = list(errs)
    sim: dict[int, dict] = {}
    good: list[dict] = []
    for op in ops:
        ing = ings.get(int(op["id"]))
        if not ing:
            errs.append(f"{op['src']}: позиции №{op['id']} нет на складе")
            continue
        bad = _sx_check_bounds(op)
        if bad:
            errs.append(f"{op['src']} (№{op['id']}): {bad}")
            continue
        cur = sim.setdefault(ing["id"], {k: float(ing[k]) for k in ("qty", "cost", "min_qty")})
        before = dict(cur)
        bad = _sx_step(cur, op)
        if bad:
            cur.update(before)
            errs.append(f"{op['src']} «{esc(ing['name'])}»: {bad}")
            continue
        good.append(op)
    L = [f"🔎 <b>Проверка правок ({esc(src)})</b>", ""]
    if sim:
        for iid, cur in sim.items():
            ing = ings[iid]
            u = esc(ing["unit"])
            parts = []
            if abs(cur["qty"] - float(ing["qty"])) > 1e-9:
                parts.append(f"остаток {_q(ing['qty'])} → <b>{_q(cur['qty'])}</b> {u}")
            if abs(cur["cost"] - float(ing["cost"])) > 1e-6:
                parts.append(f"с/с {_fmt_price(round(float(ing['cost']), 2))} → "
                             f"<b>{_fmt_price(round(cur['cost'], 2))}</b>")
            if abs(cur["min_qty"] - float(ing["min_qty"])) > 1e-9:
                parts.append(f"мин {_q(ing['min_qty'])} → <b>{_q(cur['min_qty'])}</b>")
            if parts:
                L.append(f"• №{iid} {esc(ing['name'])}: " + "; ".join(parts))
            else:
                L.append(f"• №{iid} {esc(ing['name'])}: без изменений")
    if not good:
        L.append("Изменений нет.")
    if errs:
        L += ["", f"⚠️ <b>Пропущу ({len(errs)}):</b>"] + [f"– {e}" for e in errs[:25]]
        if len(errs) > 25:
            L.append(f"…и ещё {len(errs) - 25}")
    text = "\n".join(L)
    if len(text) > 3900:  # лимит Telegram 4096
        text = text[:3850].rsplit("\n", 1)[0] + "\n…(список сокращён)"
    return text, good


def stock_apply_ops(ops: list[dict], src: str) -> tuple[list[str], list[str], list[dict]]:
    """Атомарно применяет операции к АКТУАЛЬНЫМ остаткам (за время между
    предпросмотром и ✅ могли пройти продажи). → (сделано, пропущено, затронутые)."""
    done: list[str] = []
    skipped: list[str] = []
    touched: dict[int, dict] = {}
    alert_ids: set[int] = set()   # алерт «мало» — только где менялся остаток/минимум
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for op in ops:
            r = conn.execute("SELECT * FROM ingredients WHERE id = ?", (int(op["id"]),)).fetchone()
            if not r:
                skipped.append(f"№{op['id']}: позиция удалена")
                continue
            ing = touched.get(int(r["id"])) or dict(r)
            if _sx_check_bounds(op):
                skipped.append(f"№{op['id']}: неверное значение")
                continue
            cur = {k: float(ing[k]) for k in ("qty", "cost", "min_qty")}
            bad = _sx_step(cur, op)
            if bad:
                skipped.append(f"«{esc(ing['name'])}»: {bad}")
                continue
            delta = cur["qty"] - float(ing["qty"])
            if abs(delta) > 1e-9 or abs(cur["min_qty"] - float(ing["min_qty"])) > 1e-9:
                alert_ids.add(int(ing["id"]))
            conn.execute(
                "UPDATE ingredients SET qty = ?, cost = ?, min_qty = ?, updated_at = ?"
                " WHERE id = ?", (cur["qty"], cur["cost"], cur["min_qty"], now, ing["id"]))
            if abs(delta) > 1e-9:
                reason = {"in": "приход", "out": "расход"}.get(op["op"], "инвентаризация")
                ref = src if op.get("sum") is None else f"{_fmt_price(op['sum'])} · {src}"
                conn.execute(
                    "INSERT INTO stock_moves (ingredient_id, delta, reason, ref, created_at)"
                    " VALUES (?, ?, ?, ?, ?)", (ing["id"], delta, reason, ref, now))
            label = {"in": f"+{_q(op['v'])}", "out": f"−{_q(op['v'])}",
                     "set": f"={_q(op['v'])}", "cost": f"с/с {_fmt_price(op['v'])}",
                     "min": f"мин {_q(op['v'])}"}[op["op"]]
            done.append(f"№{ing['id']} {esc(ing['name'])}: {label}")
            ing.update(cur)
            touched[int(ing["id"])] = ing
    return done, skipped, [i for k, i in touched.items() if k in alert_ids]


# ----------------------------- Excel (openpyxl) --------------------------------
_SX_COLS = ("ID", "Позиция", "Ед.", "Остаток", "С/с за ед., ₽", "Мин. остаток",
            "Вложено, ₽", "➕ Приход", "Сумма закупки, ₽", "➖ Расход",
            "Новый остаток", "Новая с/с, ₽", "Новый мин.")
# ключ → как узнать колонку при разборе (по заголовку, а не по букве)
_SX_INPUTS = (("in", "приход"), ("sum", "сумма закупки"), ("out", "расход"),
              ("set", "новый остаток"), ("cost", "новая с/с"), ("min", "новый мин"))


def _openpyxl():
    try:
        import openpyxl

        return openpyxl
    except Exception:  # noqa: BLE001
        logger.warning("Excel недоступен: добавьте openpyxl в requirements.txt")
        return None


def stock_xlsx_bytes() -> bytes:
    import io

    opx = _openpyxl()
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = opx.Workbook()
    ws = wb.active
    ws.title = "Склад"
    ws.append(list(_SX_COLS))
    head_fill = PatternFill("solid", fgColor="2B2B3A")
    in_fill = PatternFill("solid", fgColor="FFF2CC")
    ro_fill = PatternFill("solid", fgColor="F2F2F2")
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="BF8F00") if c.column >= 8 else head_fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border
    ings = db_list_ingredients()
    for ing in ings:
        q, cst = float(ing["qty"]), float(ing["cost"])
        ws.append([int(ing["id"]), ing["name"], ing["unit"], q, round(cst, 2),
                   float(ing["min_qty"]), round(q * cst), None, None, None, None, None, None])
    last = len(ings) + 1
    for row in ws.iter_rows(min_row=2, max_row=last):
        for c in row:
            c.border = border
            c.fill = in_fill if c.column >= 8 else ro_fill
            if c.column in (4, 6, 8, 10, 11, 13):
                c.number_format = "0.###"
            elif c.column in (5, 12):
                c.number_format = "#,##0.00"
            elif c.column in (7, 9):
                c.number_format = "#,##0"
    if last >= 2:
        dv = DataValidation(type="decimal", operator="between", formula1="0",
                            formula2=str(SX_MAX_SUM), allow_blank=True,
                            showErrorMessage=True, errorTitle="Только число",
                            error="Введите число ≥ 0 или оставьте пусто")
        ws.add_data_validation(dv)
        dv.add(f"H2:M{last}")
    for col, w in zip("ABCDEFGHIJKLM", (6, 34, 6, 10, 12, 12, 12, 11, 15, 11, 14, 13, 12)):
        ws.column_dimensions[col].width = w
    ws.row_dimensions[1].height = 32
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:M{last}"
    info = wb.create_sheet("Как заполнять")
    for line in (
        "Как обновить склад файлом",
        "",
        "1. Заполняйте только ЖЁЛТЫЕ колонки (H–M). Пустая ячейка — без изменений, 0 — это значение.",
        "2. «➕ Приход» — сколько пришло; «Сумма закупки, ₽» — за весь приход (с/с пересчитается средневзвешенно).",
        "3. «➖ Расход» — списание (бой, порча, угощение).",
        "4. «Новый остаток» — после пересчёта (инвентаризация). Не заполняйте вместе с приходом/расходом.",
        "5. «Новая с/с, ₽» — себестоимость за единицу; «Новый мин.» — порог «мало» (0 — не следить).",
        "6. Колонки ID и названия не меняйте. Строки можно сортировать и фильтровать.",
        "7. Сохраните файл (.xlsx) и пришлите его боту в личные сообщения.",
        "   Бот покажет, что изменится, и применит только после ✅.",
        "",
        "Новые позиции файлом не создаются — используйте «➕ Новая позиция» в /stock.",
    ):
        info.append([line])
    info["A1"].font = Font(bold=True, size=14)
    info.column_dimensions["A"].width = 110
    meta = wb.create_sheet("_zz")
    meta["A1"] = SX_MARK
    meta["A2"] = _now()
    meta.sheet_state = "hidden"
    wb.properties.title = "Zig Zag — склад"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sx_cell_num(v) -> tuple[float | None, bool]:
    """Ячейка → (число|None, ошибка?). Пусто — (None, False)."""
    if v is None:
        return None, False
    if isinstance(v, bool):
        return None, True
    if isinstance(v, (int, float)):
        return (float(v), False) if v >= 0 else (None, True)
    s = str(v).strip().replace("\u00a0", "").replace(" ", "")
    if not s or s in ("-", "—"):
        return None, False
    s = re.sub(r"(₽|руб\.?|р\.?)$", "", s)
    n = _parse_num(s)
    return (n, False) if n is not None else (None, True)


def sx_parse_xlsx(data: bytes) -> tuple[list[dict], list[str]]:
    """Excel-файл склада → (операции, ошибки)."""
    import io

    opx = _openpyxl()
    try:
        wb = opx.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 — не xlsx / повреждён
        return [], ["Не удалось открыть файл. Нужен .xlsx (Excel / Google Таблицы → .xlsx)."]
    try:
        ws = wb["Склад"] if "Склад" in wb.sheetnames else wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None) or ()
        norm = [re.sub(r"[^\wа-яё/ ]", "", str(h or "").lower()).strip() for h in header]
        col: dict[str, int] = {}
        for i, h in enumerate(norm):
            if h == "id":
                col["id"] = i
            elif h == "позиция":
                col["name"] = i
            for key, needle in _SX_INPUTS:
                # «Расход» ≠ «приход»: сравниваем начало, «Новый остаток» ≠ «Остаток»
                if h.startswith(needle) and key not in col:
                    col[key] = i
        if "id" not in col or not any(k in col for k, _ in _SX_INPUTS):
            return [], ["Это не таблица склада: нет колонок «ID» и «➕ Приход»… "
                        "Скачайте шаблон кнопкой «📥 Скачать Excel» в /stock."]
        ings = {int(i["id"]): i for i in db_list_ingredients()}
        ops: list[dict] = []
        errs: list[str] = []
        for rn, row in enumerate(rows, start=2):
            if rn > 5000:
                errs.append("Файл длиннее 5000 строк — остальное не читал.")
                break
            row = tuple(row or ())

            def get(key):
                i = col.get(key)
                return row[i] if i is not None and i < len(row) else None

            raw_id = get("id")
            vals: dict[str, float] = {}
            bad_cells = []
            for key, _ in _SX_INPUTS:
                v, bad = _sx_cell_num(get(key))
                if bad:
                    bad_cells.append(_SX_COLS[7 + [k for k, _ in _SX_INPUTS].index(key)])
                elif v is not None:
                    vals[key] = v
            if raw_id is None and not vals and not bad_cells:
                continue  # пустая строка
            src = f"строка {rn}"
            try:
                iid = int(float(raw_id))
            except (TypeError, ValueError):
                if vals or bad_cells:
                    errs.append(f"{src}: нет ID позиции")
                continue
            if bad_cells:
                errs.append(f"{src} (ID {iid}): не число в «{', '.join(bad_cells)}»")
                continue
            if not vals:
                continue
            ing = ings.get(iid)
            fname = str(get("name") or "").strip()
            if ing and fname and fname != ing["name"]:
                errs.append(f"{src}: ID {iid} в базе «{esc(ing['name'])}», в файле "
                            f"«{esc(fname[:40])}» — строку пропустил (сверьте ID)")
                continue
            if "set" in vals and ("in" in vals or "out" in vals):
                errs.append(f"{src} (ID {iid}): «Новый остаток» вместе с приходом/расходом — "
                            "оставьте что-то одно")
                continue
            if "sum" in vals and "in" not in vals:
                errs.append(f"{src} (ID {iid}): сумма закупки без количества прихода")
                continue
            if "in" in vals:
                ops.append({"id": iid, "op": "in", "v": vals["in"], "sum": vals.get("sum"),
                            "src": src})
            if "out" in vals:
                ops.append({"id": iid, "op": "out", "v": vals["out"], "src": src})
            if "set" in vals:
                ops.append({"id": iid, "op": "set", "v": vals["set"], "cost": None, "src": src})
            if "cost" in vals:
                ops.append({"id": iid, "op": "cost", "v": vals["cost"], "src": src})
            if "min" in vals:
                ops.append({"id": iid, "op": "min", "v": vals["min"], "src": src})
            if len(ops) > SX_MAX_OPS:
                return [], [f"Слишком много изменений (>{SX_MAX_OPS}) — разбейте на части."]
        return ops, errs
    finally:
        wb.close()


def kb_sx_confirm(n: int) -> InlineKeyboardMarkup:
    rows = []
    if n:
        rows.append([_btn(f"✅ Применить ({n})", "sx:apply")])
    rows.append([_btn("✖ Отмена", "sx:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _sx_show_preview(message: Message, state: FSMContext, ops: list[dict],
                           errs: list[str], src: str) -> None:
    text, good = sx_preview(ops, errs, src)
    if good:
        await state.set_state(StockBulkFSM.confirm)
        await state.update_data(sx_ops=good, sx_src=src)
    else:
        await state.clear()
    await message.answer(text, reply_markup=kb_sx_confirm(len(good)) if good else
                         InlineKeyboardMarkup(inline_keyboard=[
                             [_btn("✏️ Ещё раз", "sx:bulk"), _btn("📋 Таблица", "sx:table")]]))


async def _send_stock_table(message: Message) -> None:
    texts = stock_table_texts()
    for i, t in enumerate(texts):
        await message.answer(t, reply_markup=kb_stock_table() if i == len(texts) - 1 else None)


@router.callback_query(F.data.startswith("sx:"))
async def cb_stock_table(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("⛔ Только для администратора", show_alert=True)
        return
    act = cb.data.split(":", 1)[1]
    try:
        if act == "table":
            await state.clear()
            await cb.answer()
            texts = stock_table_texts()
            if len(texts) == 1:
                try:
                    await cb.message.edit_text(texts[0], reply_markup=kb_stock_table())
                    return
                except TelegramBadRequest:
                    pass
            await _send_stock_table(cb.message)
            return
        if act == "bulk":
            await state.set_state(StockBulkFSM.text)
            ings = db_list_ingredients()
            await cb.answer()
            await cb.message.answer(_sx_help(ings[0]["id"] if ings else None),
                                    reply_markup=_cancel_kb("sx:cancel"))
            return
        if act == "xlsx":
            if not _openpyxl():
                await cb.answer("Excel недоступен: на сервере нет openpyxl "
                                "(добавьте в requirements.txt)", show_alert=True)
                return
            await cb.answer("Готовлю файл…")
            data = await asyncio.to_thread(stock_xlsx_bytes)
            await state.clear()
            await cb.message.answer_document(
                BufferedInputFile(data, f"zigzag_stock_{_now_dt():%Y-%m-%d}.xlsx"),
                caption="📥 Склад в Excel. Заполните <b>жёлтые</b> колонки (пусто — без "
                        "изменений), сохраните и пришлите файл сюда — покажу, что "
                        "изменится, и применю после ✅. Подробно — на листе «Как заполнять».",
            )
            return
        if act == "cancel":
            await state.clear()
            await cb.answer("Отменили")
            try:
                await cb.message.edit_text("✖ Правки склада отменены.",
                                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                               [_btn("📋 Таблица", "sx:table")]]))
            except TelegramBadRequest:
                pass
            return
        if act == "apply":
            if await state.get_state() != StockBulkFSM.confirm:
                await cb.answer("Предпросмотр устарел — пришлите правки заново", show_alert=True)
                return
            data = await state.get_data()
            ops = data.get("sx_ops") or []
            src = data.get("sx_src") or "таблица"
            await state.clear()  # повторное нажатие ничего не применит дважды
            done, skipped, touched = stock_apply_ops(ops, src)
            await cb.answer("Готово ✅" if done else "Ничего не применено")
            L = [f"✅ <b>Склад обновлён ({esc(src)})</b>: {len(done)} изм.", ""]
            L += [f"• {d}" for d in done[:40]]
            if len(done) > 40:
                L.append(f"…и ещё {len(done) - 40}")
            if skipped:
                L += ["", "⚠️ <b>Не применено</b> (остатки изменились после проверки):"]
                L += [f"– {s}" for s in skipped[:20]]
            try:
                await cb.message.edit_text(
                    "\n".join(L)[:4000],
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [_btn("📋 Таблица", "sx:table"), _btn("✏️ Ещё правки", "sx:bulk")]]))
            except TelegramBadRequest:
                pass
            if touched:
                await _stock_alerts(cb.bot, touched)
            return
        await cb.answer()
    except Exception:
        logger.exception("Ошибка в склад-таблице: %s", cb.data)
        try:
            await cb.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.message(StockBulkFSM.text, F.text, ~F.text.startswith("/"))
async def stock_bulk_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    ops, errs = sx_parse_text(message.text or "")
    if not ops and not errs:
        await message.answer("Пусто. Пришлите правки или ✖ Отмена.")
        return
    await _sx_show_preview(message, state, ops, errs, "текстом")


@router.message(F.document, ~F.forward_origin)
async def stock_xlsx_upload(message: Message, state: FSMContext):
    """Админ прислал заполненный Excel склада."""
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    doc = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith(".xlsx"):
        await message.answer("📎 Склад принимаю только файлом .xlsx — шаблон: /stock → "
                             "«📥 Скачать Excel».")
        return
    if (doc.file_size or 0) > SX_MAX_FILE:
        await message.answer("Файл больше 2 МБ — это точно таблица склада?")
        return
    if not _openpyxl():
        await message.answer("Excel недоступен: на сервере нет openpyxl "
                             "(добавьте в requirements.txt).")
        return
    try:
        buf = await message.bot.download(doc)
        data = buf.read() if buf else b""
    except Exception:
        logger.exception("Не удалось скачать файл склада")
        await message.answer("Не удалось скачать файл, пришлите ещё раз.")
        return
    if len(data) > SX_MAX_FILE:
        await message.answer("Файл больше 2 МБ — это точно таблица склада?")
        return
    ops, errs = await asyncio.to_thread(sx_parse_xlsx, data)
    if not ops and not errs:
        await state.clear()
        await message.answer("В файле нет изменений: жёлтые колонки пустые.")
        return
    await _sx_show_preview(message, state, ops, errs, "Excel")


# ------------------------------------------- /recipe -------------------------
def kb_rct_categories() -> InlineKeyboardMarkup:
    rows = [
        [_btn(f"{c['emoji']} {c['name']}", f"rct:{c['id']}")]
        for c in MENU["categories"]
    ]
    rows.append([_btn("✖ Закрыть", "rct:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_rct_items(cat_id: str) -> InlineKeyboardMarkup:
    cat = f_category(cat_id) or {"items": []}
    rows = []
    for it in cat["items"]:
        mark = "" if item_in_stock(it["id"]) else " ⛔"
        rows.append([_btn(f"{it['name']}{mark}", f"rct:i:{it['id']}")])
    rows.append([_btn("🔙 Разделы", "rct")])
    rows.append([_btn("✖ Закрыть", "rct:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rct_view_text(item_id: str) -> str:
    it = _menu_item(item_id)
    name = it["name"] if it else item_id
    recs = db_recipes_for_item(item_id)
    lines = [
        f"📖 <b>Рецепт: {esc(name)}</b>",
        "Норма на <b>1 порцию</b>:",
        "",
    ]
    if not recs:
        lines.append("Пока пусто — при оплате заказа этот товар не спишется.")
    else:
        for r in recs:
            lines.append(f"• {esc(r['name'])} — {_fmt_qty(r['need'])} {esc(r['unit'])}")
    return "\n".join(lines)


def kb_rct_view(item_id: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for r in db_recipes_for_item(item_id):
        rows.append(
            [
                _btn(f"{r['name']} · {_fmt_qty(r['need'])} {r['unit']}", "noop"),
                _btn("🗑", f"rct:d:{item_id}:{r['id']}"),
            ]
        )
    rows.append([_btn("➕ Ингредиент", f"rct:a:{item_id}")])
    cat = _category_of_item(item_id)
    if cat:
        rows.append([_btn("🔙 К позициям", f"rct:{cat}")])
    rows.append([_btn("✖ Закрыть", "rct:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("recipe"))
async def cmd_recipe(message: Message, state: FSMContext):
    """Рецепты: из чего списывать позиции меню."""
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await message.answer(
        "📖 <b>Рецепты</b>\n\nВыберите раздел меню:", reply_markup=kb_rct_categories()
    )


@router.callback_query(F.data.startswith("rct"))
async def cb_recipe(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("⛔ Только для администратора", show_alert=True)
        return
    parts = cb.data.split(":")
    try:
        if cb.data == "rct":  # разделы
            await cb.answer()
            await cb.message.edit_text(
                "📖 <b>Рецепты</b>\n\nВыберите раздел меню:",
                reply_markup=kb_rct_categories(),
            )
            return
        if parts[1] == "cancel":
            data = await state.get_data()
            await state.clear()
            await cb.answer("Отменили")
            item_id = data.get("rct_item")
            if item_id:
                try:
                    await cb.message.edit_text(
                        rct_view_text(item_id), reply_markup=kb_rct_view(item_id)
                    )
                except TelegramBadRequest:
                    pass
            return
        if parts[1] == "close":
            await state.clear()
            await cb.answer("Закрыли")
            try:
                await cb.message.edit_text("✅ Готово", reply_markup=None)
            except TelegramBadRequest:
                pass
            return
        if len(parts) == 2:  # раздел → позиции
            cat = f_category(parts[1])
            if not cat:
                await cb.answer("Раздел не найден", show_alert=True)
                return
            await cb.answer()
            await cb.message.edit_text(
                f"{cat['emoji']} <b>{esc(cat['name'])}</b> — выберите позицию:",
                reply_markup=kb_rct_items(cat["id"]),
            )
            return
        if parts[1] == "i":  # карточка рецепта
            await cb.answer()
            await cb.message.edit_text(
                rct_view_text(parts[2]), reply_markup=kb_rct_view(parts[2])
            )
            return
        if parts[1] == "a":  # выбор ингредиента
            item_id = parts[2]
            ings = db_list_ingredients()
            if not ings:
                await cb.answer()
                await cb.message.edit_text(
                    "На складе пока нет ингредиентов.\n"
                    "Сначала добавьте их в /stock — потом вернитесь сюда.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[_btn("🔙 Назад", f"rct:i:{item_id}")]]
                    ),
                )
                return
            rows = [
                [_btn(f"{i['name']} · {i['unit']}", f"rct:p:{item_id}:{i['id']}")]
                for i in ings
            ]
            rows.append([_btn("🔙 Назад", f"rct:i:{item_id}")])
            await cb.answer()
            await cb.message.edit_text(
                "➕ Какой ингредиент добавить?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            return
        if parts[1] == "p":  # ввод количества на порцию
            item_id = parts[2]
            ing = db_get_ingredient(int(parts[3]))
            if not ing:
                await cb.answer("Ингредиент не найден", show_alert=True)
                return
            it = _menu_item(item_id)
            await cb.answer()
            await state.set_state(RecipeFSM.qty)
            await state.update_data(
                rct_item=item_id,
                rct_ing=ing["id"],
                ed_msg=cb.message.message_id,
            )
            await cb.message.edit_text(
                f"«{esc(it['name'] if it else item_id)}» + <b>{esc(ing['name'])}</b>\n\n"
                f"Сколько {esc(ing['unit'])} на 1 порцию? Отправьте числом:",
                reply_markup=_cancel_kb("rct:cancel"),
            )
            return
        if parts[1] == "d":  # убрать ингредиент из рецепта
            item_id = parts[2]
            db_delete_recipe(item_id, int(parts[3]))
            await cb.answer("Убрали из рецепта")
            await cb.message.edit_text(
                rct_view_text(item_id), reply_markup=kb_rct_view(item_id)
            )
            return
    except Exception:
        logger.exception("Ошибка в колбэке рецепта: %s", cb.data)
        try:
            await cb.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.message(RecipeFSM.qty, F.text)
async def recipe_qty_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    data = await state.get_data()
    item_id = data.get("rct_item")
    ing = db_get_ingredient(data.get("rct_ing") or 0)
    v = _parse_num((message.text or "").strip())
    if v is None or v <= 0 or v > 1_000_000:
        await message.answer(
            "Введите положительное число (например 10), либо ✖ Отмена кнопкой выше."
        )
        return
    await state.clear()
    if not item_id or not ing:
        await message.answer("Не удалось сохранить — откройте /recipe заново.")
        return
    db_upsert_recipe(item_id, ing["id"], v)
    await _edit_or_send(
        message,
        data,
        f"✅ {esc(ing['name'])} — {_fmt_qty(v)} {esc(ing['unit'])} на порцию\n\n"
        + rct_view_text(item_id),
        kb_rct_view(item_id),
    )


@router.message(Command("bookings"))
async def cmd_bookings(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    res = db_upcoming_reservations(10)
    if not res:
        await message.answer("Ближайших броней нет.")
        return
    lines = ["📅 Ближайшие брони:", ""]
    for r in res:
        kind = RSV_KINDS.get(r["kind"], r["kind"])
        status = RSV_STATUSES.get(r["status"], r["status"])
        d = r["date"][8:10] + "." + r["date"][5:7]
        lines.append(
            f"#{r['id']} · {kind} · {d} {r['time']} · "
            f"{r['guests']} {_guests_word(r['guests'])} · {status}"
        )
    await message.answer("\n".join(lines))


# -------------------------------- кнопки статусов в канале -------------------
_admin_cache: dict[int, tuple[bool, float]] = {}
_ADMIN_CACHE_TTL = 300  # сек


async def _is_channel_admin(bot, user_id: int) -> bool:
    """Админ канала персонала или админ из ADMIN_IDS.
    При ЛЮБОЙ ошибке проверки — отказ (fail-closed), результат ошибки не кэшируем."""
    if _is_admin(user_id):
        return True
    if not CHANNEL_ID:
        return False
    now = time.monotonic()
    cached = _admin_cache.get(user_id)
    if cached and now - cached[1] < _ADMIN_CACHE_TTL:
        return cached[0]
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        ok = member.status in ("creator", "administrator")
    except Exception as e:
        logger.warning("Проверка прав %s в канале не удалась (%s) — отказ", user_id, e)
        return False
    _admin_cache[user_id] = (ok, now)
    return ok


def _from_staff_chat(cb: CallbackQuery) -> bool:
    """Нажатие пришло из канала персонала (а не подделано в личке с ботом)."""
    chat = getattr(cb.message, "chat", None) if cb.message else None
    if chat is None or not CHANNEL_ID:
        return False
    if isinstance(CHANNEL_ID, int):
        return chat.id == CHANNEL_ID
    uname = (getattr(chat, "username", None) or "").lower()
    return bool(uname) and uname == str(CHANNEL_ID).lstrip("@").lower()


async def _staff_guard(cb: CallbackQuery, deny: str) -> bool:
    """True — можно выполнять действие персонала. Иначе отвечает отказом."""
    if not _from_staff_chat(cb):
        await cb.answer("⛔ Действие доступно только в канале персонала", show_alert=True)
        return False
    if not await _is_channel_admin(cb.bot, cb.from_user.id):
        await cb.answer(deny, show_alert=True)
        return False
    return True


@router.callback_query(F.data.startswith("st:"))
async def cb_status(cb: CallbackQuery):
    try:
        _, oid_raw, code = cb.data.split(":")
        order_id = int(oid_raw)
    except (ValueError, AttributeError):
        await cb.answer("Некорректный запрос", show_alert=True)
        return

    if code not in STATUSES:
        await cb.answer("Неизвестный статус", show_alert=True)
        return

    if not await _staff_guard(cb, "⚠️ Статус меняют только администраторы канала"):
        return

    order = db_get_order(order_id)
    if not order:
        await cb.answer("Заказ не найден (возможно, удалён)", show_alert=True)
        return

    label = STATUSES[code]
    if order["status"] == code:
        await cb.answer(f"Уже: {label}")
        return

    # Заказ уже оплачен — прочие статусы не ставим.
    # Выход только один: ❌ Отменён (возврат / перерасчёт).
    if order["status"] == "paid" and code != "cancelled":
        await cb.answer(
            "🔒 Заказ уже оплачен — статус изменить нельзя. "
            "Возврат/перерасчёт: ❌ Отменён",
            show_alert=True,
        )
        return

    # «Оплачен» — сначала спрашиваем у админа, как была произведена оплата
    if code == "paid":
        try:
            await cb.message.edit_reply_markup(reply_markup=kb_pay(order_id))
        except TelegramBadRequest:
            pass
        await cb.answer("💵 Уточните, как была произведена оплата")
        return

    db_set_status(order_id, code)
    order = db_get_order(order_id)

    refund = None
    if code == "cancelled":
        # возврат ингредиентов на склад (ровно один раз — атомарный флаг)
        if db_claim_stock_flag(order_id, 0):
            affected = stock_apply_order(order, +1)
            await _stock_alerts(cb.bot, affected)
        # возврат баллов / отмена кешбэка (ровно один раз)
        refund = loyalty_on_refund(order_id)
        db_set_bonus_pending(order_id, 0)
        order = db_get_order(order_id)

    try:
        await cb.message.edit_text(order_text(order), reply_markup=kb_status(order_id))
    except Exception:
        logger.exception("Не удалось отредактировать сообщение заказа #%s", order_id)

    # --- уведомление гостю о смене статуса заказа ---
    if order.get("user_id"):
        note = _order_notify_text(order)
        if refund and (refund["returned"] or refund["taken"]):
            note += (
                f"\n💎 Баллы: возвращено {_fmt_pts(refund['returned'])},"
                f" списан кешбэк {_fmt_pts(refund['taken'])}."
                f" Баланс: {_fmt_pts(refund['balance'] or 0)}"
            )
        try:
            await cb.bot.send_message(order["user_id"], note)
        except Exception:
            logger.info(
                "Не удалось уведомить гостя заказа #%s (user_id=%s)",
                order_id,
                order.get("user_id"),
            )

    who = cb.from_user.username or cb.from_user.full_name
    suffix = f" ({who})" if who and not str(cb.from_user.id).startswith("-") else ""
    await cb.answer(f"{label}{suffix}")


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cb: CallbackQuery):
    """Выбор способа оплаты: нал / безнал / перевод, баллы (или «Назад»)."""
    try:
        _, oid_raw, action = cb.data.split(":")
        order_id = int(oid_raw)
    except (ValueError, AttributeError):
        await cb.answer("Некорректный запрос", show_alert=True)
        return

    if not await _staff_guard(cb, "⚠️ Оплату подтверждают только администраторы канала"):
        return

    order = db_get_order(order_id)
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return

    if action == "back":
        db_set_bonus_pending(order_id, 0)
        order = db_get_order(order_id)
        try:
            await cb.message.edit_text(order_text(order), reply_markup=kb_status(order_id))
        except TelegramBadRequest:
            pass
        await cb.answer("Отменили")
        return

    if action in ("bonus", "nobonus"):
        if order["status"] == "paid":
            await cb.answer("Заказ уже оплачен", show_alert=True)
            return
        if action == "bonus":
            pts = bonus_available_for_order(order)
            if pts <= 0:
                await cb.answer("Баллов для списания нет", show_alert=True)
                return
            db_set_bonus_pending(order_id, pts)
            msg = f"🎁 Спишем {_fmt_pts(pts)} баллов · к оплате {_fmt_money(order['total'] - pts)}. Выберите способ оплаты"
        else:
            db_set_bonus_pending(order_id, 0)
            msg = "Баллы не списываем — выберите способ оплаты"
        order = db_get_order(order_id)
        try:
            await cb.message.edit_text(order_text(order), reply_markup=kb_pay(order_id))
        except TelegramBadRequest:
            pass
        await cb.answer(msg, show_alert=action == "bonus")
        return

    if action not in PAY_METHODS:
        await cb.answer("Неизвестный способ оплаты", show_alert=True)
        return

    if not db_claim_paid(order_id, action):
        # уже оплачен — только поправить способ, без повторного списания
        db_set_pay_method(order_id, action)
        order = db_get_order(order_id)
        try:
            await cb.message.edit_text(order_text(order), reply_markup=kb_status(order_id))
        except Exception:
            logger.exception("Не удалось обновить заказ #%s", order_id)
        await cb.answer(f"Способ оплаты: {PAY_METHODS[action]} (уже оплачен)")
        return

    # мы перевели заказ в «Оплачен» — баллы и склад ровно один раз
    loyal = loyalty_on_paid(order_id)
    if db_claim_stock_flag(order_id, 1):
        affected = stock_apply_order(order, -1)
        await _stock_alerts(cb.bot, affected)
    order = db_get_order(order_id)

    try:
        await cb.message.edit_text(order_text(order), reply_markup=kb_status(order_id))
    except Exception:
        logger.exception("Не удалось обновить заказ #%s после оплаты", order_id)

    # уведомляем гостя
    if order.get("user_id"):
        note = _order_notify_text(order)
        if loyal["spent"] or loyal["accrued"]:
            parts = []
            if loyal["spent"]:
                parts.append(f"списано {_fmt_pts(loyal['spent'])}")
            if loyal["accrued"]:
                parts.append(f"начислено +{_fmt_pts(loyal['accrued'])}")
            note += f"\n💎 Баллы: {', '.join(parts)}. Баланс: {_fmt_pts(loyal['balance'] or 0)}"
        try:
            await cb.bot.send_message(order["user_id"], note)
        except Exception:
            logger.info("Не удалось уведомить гостя заказа #%s", order_id)

    tail = f" · −{_fmt_pts(loyal['spent'])} баллами" if loyal["spent"] else ""
    await cb.answer(f"Оплата: {PAY_METHODS[action]} ✅{tail}")


# ------------------------------------------------------ /start ---------------
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    # Поддержка QR-ссылки t.me/bot?start=5 или start=table5 — стол запоминаем сразу
    if command.args:
        m = re.search(r"\d+", command.args)
        if m and 1 <= int(m.group()) <= 8:
            await state.update_data(table=str(int(m.group())))
    await state.set_state(None)
    await message.answer(WELCOME, reply_markup=kb_welcome())


@router.message(Command("table"))
async def cmd_table(message: Message, state: FSMContext):
    """Сменить стол."""
    await state.set_state(OrderFSM.table)
    await message.answer(ASK_TABLE)


# ----------------------------------------------------- ввод стола ------------
@router.message(OrderFSM.table)
async def on_table_input(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 8:
        await message.answer(ERR_TABLE)
        return
    table = str(int(raw))
    await state.update_data(table=table)
    await state.set_state(None)
    await message.answer(table_set_text(table), reply_markup=kb_main(table))


# ====================== СЦЕНАРИЙ: ВЫБОР → ЛОЯЛЬНОСТЬ ========================
# /start → 2 кнопки (бронь / заказ) → первый раз или постоянный → телефон →
# дальше по выбранному сценарию.


def kb_welcome() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📅 Забронировать стол / VIP", "go:rsv")],
            [_btn("🛒 Сделать заказ", "go:order")],
            [_btn("📝 Предзаказ к приходу", "go:pre")],
        ]
    )


def kb_loyal() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🌟 Первый раз", "loyal:first")],
            [_btn("💎 Постоянный гость", "loyal:reg")],
            [_btn("🔙 Назад", "go:home")],
        ]
    )


def kb_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить мой номер", request_contact=True)],
            [KeyboardButton(text=PHONE_SKIP)],
            [KeyboardButton(text=PHONE_BACK)],
        ],
        resize_keyboard=True,
        input_field_placeholder="+7 900 123-45-67",
    )


async def _back_to_welcome(message: Message, state: FSMContext) -> None:
    """Вернуть гостя на экран выбора (бронь/заказ), убрав клавиатуру телефона."""
    await state.set_state(None)
    await message.answer(WELCOME, reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите действие 👇", reply_markup=kb_welcome())


async def _proceed(target: str, message: Message, state: FSMContext) -> None:
    """Продолжение сценария после лояльности: 'rsv' — бронь, иначе — заказ."""
    data = await state.get_data()
    if target == "card":
        await _send_card(message, state, message.chat.id)
        return
    if target == "pre":
        await _pre_home(message, state, message.chat.id)
        return
    if target == "rsv":
        await state.update_data(rsv={})
        await message.answer(
            "📅 <b>Бронирование</b>\n\nЧто будем бронировать?",
            reply_markup=kb_rsv_kind(),
        )
        return
    # --- заказ ---
    table = data.get("table")
    if table:
        await message.answer(main_text(table), reply_markup=kb_main(table))
        await message.answer(
            "📋 Выберите раздел:",
            reply_markup=kb_categories(_cart_count(data.get("cart") or [])),
        )
    else:
        await state.set_state(OrderFSM.table)
        await message.answer(ASK_TABLE, reply_markup=ReplyKeyboardRemove())


async def _begin_flow(
    target: str,
    message: Message,
    state: FSMContext,
    cb: CallbackQuery | None = None,
    strip: bool = True,
) -> None:
    """Старт сценария (бронь/заказ): сначала лояльность, если карты ещё нет
    и гость не пропускал вопрос в этой сессии."""
    user = cb.from_user if cb is not None else message.from_user
    if cb is not None:
        if strip:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)  # убрать кнопки приветствия
            except TelegramBadRequest:
                pass
        await cb.answer()
    await state.update_data(next=target)
    data = await state.get_data()
    card = guest_card(user.id if user else None)
    if card:
        db_guest_touch(user)
        if not data.get("card_greeted"):
            await state.update_data(card_greeted=True)
            await message.answer(
                f"💎 Карта <b>{esc(card['phone'])}</b> · баллов: "
                f"<b>{_fmt_pts(db_bonus_balance(user.id))}</b>"
            )
        await _proceed(target, message, state)
    elif data.get("loyal_done"):
        await _proceed(target, message, state)
    else:
        await message.answer(LOYAL_ASK, reply_markup=kb_loyal())


async def _finish_phone(
    phone: str | None, message: Message, state: FSMContext, verified: bool = False
) -> None:
    data = await state.get_data()
    if phone:
        ok, why = db_guest_set_phone(message.from_user, phone, verified)
        if not ok:
            await message.answer(
                "⚠️ Этот номер уже привязан к другой карте.\n\n"
                "Если это ваш номер — отправьте его кнопкой «📱 Отправить мой номер» "
                "(подтверждение через Telegram) или обратитесь к администратору. "
                "Можно также «⏭ Пропустить».",
                reply_markup=kb_phone(),
            )
            return  # остаёмся на шаге телефона
    await state.update_data(phone=phone, loyal_done=True, card_greeted=bool(phone))
    await state.set_state(None)
    if phone:
        bal = db_bonus_balance(message.from_user.id)
        await message.answer(
            f"✅ Карта лояльности привязана! Номер карты: <b>{esc(phone)}</b>\n"
            f"🎁 Баллов: <b>{_fmt_pts(bal)}</b> · кешбэк {LOYALTY_PERCENT}% с каждого "
            f"оплаченного заказа. Карта — кнопка «{BTN_CARD}».",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await message.answer(
            "Хорошо, пропустили 👌 Без номера баллы не начисляются — "
            f"подключить карту можно позже кнопкой «{BTN_CARD}».",
            reply_markup=ReplyKeyboardRemove(),
        )
    await _proceed(data.get("next") or "order", message, state)


@router.callback_query(F.data.startswith("go:"))
async def go_choice(cb: CallbackQuery, state: FSMContext):
    try:
        target = cb.data.split(":", 1)[1]  # "go:rsv" → "rsv"
        if target == "home":
            # «Назад» — вернуться на экран выбора бронь/заказ
            await cb.answer()
            await cb.message.edit_text(WELCOME, reply_markup=kb_welcome())
            return
        if target not in ("rsv", "order", "pre"):
            await cb.answer("Ошибка выбора", show_alert=True)
            return
        await _begin_flow(target, cb.message, state, cb=cb)
    except Exception:
        logger.exception("Ошибка в go_choice (data=%s)", cb.data)
        try:
            await cb.answer("⚠️ Ошибка — посмотрите логи бота на хостинге", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("loyal:"))
async def loyal_pick(cb: CallbackQuery, state: FSMContext):
    try:
        kind = cb.data.split(":", 1)[1]  # "loyal:first" → "first"
        if kind not in ("first", "reg"):
            await cb.answer("Ошибка", show_alert=True)
            return
        await state.update_data(loyal_type=kind)
        await cb.answer()
        prompt = LOYAL_FIRST if kind == "first" else LOYAL_REG
        try:
            await cb.message.edit_text(prompt)  # убираем кнопки выбора
        except TelegramBadRequest:
            pass
        await cb.message.answer(PHONE_ASK, reply_markup=kb_phone())
        await state.set_state(OrderFSM.phone)
    except Exception:
        logger.exception("Ошибка в loyal_pick (data=%s)", cb.data)
        try:
            await cb.answer("⚠️ Ошибка — посмотрите логи бота на хостинге", show_alert=True)
        except Exception:
            pass


@router.message(OrderFSM.phone, F.contact)
async def phone_contact(message: Message, state: FSMContext):
    if not message.contact:
        return
    # принимаем как подтверждённый только СВОЙ контакт (кнопка «Отправить мой номер»)
    if not message.from_user or message.contact.user_id != message.from_user.id:
        await message.answer(
            "⚠️ Это контакт другого человека. Отправьте свой номер кнопкой "
            "«📱 Отправить мой номер» или напишите его текстом.",
            reply_markup=kb_phone(),
        )
        return
    phone = normalize_phone(message.contact.phone_number)
    if not phone:
        await message.answer("Не удалось распознать номер — напишите его текстом.")
        return
    await _finish_phone(phone, message, state, verified=True)


@router.message(OrderFSM.phone, F.text)
async def phone_text(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    if t == PHONE_BACK:
        await _back_to_welcome(message, state)
        return
    if t == PHONE_SKIP:
        await _finish_phone(None, message, state)
        return
    phone = normalize_phone(t)
    if phone:
        await _finish_phone(phone, message, state, verified=False)
        return
    await message.answer(
        "Похоже, это не номер 😅\n\nПришлите его кнопкой «📱 Отправить мой номер», "
        "напишите в чат (например +7 900 123-45-67), нажмите «⏭ Пропустить» "
        "или «🔙 Назад», чтобы вернуться к выбору."
    )


@router.message(OrderFSM.phone)
async def phone_other(message: Message, state: FSMContext):
    await message.answer(
        "📱 Пришлите номер: кнопкой «📱 Отправить мой номер», текстом "
        "или нажмите «⏭ Пропустить»."
    )


# --- ввод имени, на кого бронь (шаг между гостями и длительностью) ---
@router.message(OrderFSM.rsv_name, F.text)
async def rsv_name_input(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    if t in (BTN_MENU, BTN_CART, BTN_BOOK, BTN_CARD, BTN_PRE):
        await message.answer("✍️ Введите имя для брони обычным сообщением")
        return
    if not t or len(t) > 120:
        await message.answer("Введите имя (до 120 символов) сообщением ✍️")
        return
    data = await state.get_data()
    rsv = dict(data.get("rsv") or {})
    rsv["name"] = t
    await state.update_data(rsv=rsv)
    await state.set_state(None)
    # отметим экран ввода имени
    msg_id = data.get("rsv_name_msg")
    if msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_id,
                text=f"✍️ Имя: <b>{esc(t)}</b> ✅",
            )
        except TelegramBadRequest:
            pass
    # 🍸 барная стойка — бронь места без часов: сразу к подтверждению
    if rsv.get("kind") == "bar":
        nm = rsv["name"]
        if message.from_user and message.from_user.username and "(@" not in nm:
            nm += f" (@{message.from_user.username})"
        await message.answer(
            _rsv_confirm_text(rsv, nm), reply_markup=kb_rsv_confirm(rsv)
        )
        return
    text, kb = _rsv_dur_screen(rsv)
    await message.answer(text, reply_markup=kb)


@router.message(OrderFSM.rsv_name)
async def rsv_name_other(message: Message, state: FSMContext):
    await message.answer("✍️ Пришлите имя обычным текстовым сообщением")


# ------------------------------------------- кнопки reply-меню ---------------
@router.message(F.text == BTN_MENU)
async def on_btn_menu(message: Message, state: FSMContext):
    if not await _table(state):
        await state.set_state(OrderFSM.table)
        await message.answer(ASK_TABLE)
        return
    await _show_menu(message, state)


@router.message(F.text == BTN_CART)
async def on_btn_cart(message: Message, state: FSMContext):
    cart = await _cart(state)
    table = await _table(state)
    if not cart:
        await message.answer(CART_EMPTY, reply_markup=kb_main(table))
        return
    await message.answer(cart_text(cart, table), reply_markup=kb_cart(cart))


@router.message(F.text.startswith("🪑 Стол:"))
async def on_btn_change_table(message: Message, state: FSMContext):
    await state.set_state(OrderFSM.table)
    await message.answer(ASK_TABLE)


# ------------------------------------------------- навигация по меню ---------
@router.callback_query(F.data == "m")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _show_menu(cb.message, state, edit=True)


_photo_file_ids: dict[str, str] = {}  # кэш file_id: фото грузится в Telegram один раз


async def _send_info_photo(cb: CallbackQuery, which: str) -> None:
    """Фото «Выгода»/«Правила»: файл рядом с bot.py, иначе встроенная копия.
    aiogram 3 принимает только InputFile/str — сырой файл/BytesIO не годится."""
    path, b64 = (PROMO_PHOTO, PROMO_PHOTO_B64) if which == "promo" else (RULES_PHOTO, RULES_PHOTO_B64)
    kb = kb_info_photo(promo=(which == "promo"))
    fid = _photo_file_ids.get(which)
    if fid:
        try:
            await cb.message.answer_photo(fid, reply_markup=kb)
            return
        except TelegramBadRequest:
            _photo_file_ids.pop(which, None)
    photo = FSInputFile(path) if path.is_file() else BufferedInputFile(
        base64.b64decode(b64), f"{which}.jpg"
    )
    sent = await cb.message.answer_photo(photo, reply_markup=kb)
    try:
        _photo_file_ids[which] = sent.photo[-1].file_id
    except (AttributeError, IndexError, TypeError):
        pass


@router.callback_query(F.data == "info:benefit")
async def cb_info_benefit(cb: CallbackQuery):
    await cb.answer()
    try:
        await _send_info_photo(cb, "promo")
    except Exception:
        logger.exception("Не удалось отправить фото «Выгода»")


@router.callback_query(F.data == "info:rules")
async def cb_info_rules(cb: CallbackQuery):
    await cb.answer()
    try:
        await _send_info_photo(cb, "rules")
    except Exception:
        logger.exception("Не удалось отправить фото «Правила»")


@router.callback_query(F.data == "info:close")
async def cb_info_close(cb: CallbackQuery):
    await cb.answer("Меню выше 👇")
    try:
        await cb.message.delete()
    except Exception:
        logger.info("Не удалось удалить фото-сообщение (info:close)")


@router.callback_query(F.data.startswith("c:"))
async def cb_category(cb: CallbackQuery, state: FSMContext):
    cat = f_category(cb.data[2:])
    if not cat:
        await cb.answer(ERR_MENU, show_alert=True)
        return
    await cb.answer()
    cart = await _cart(state)
    await cb.message.edit_text(
        f"{cat['emoji']} <b>{esc(cat['name'])}</b> — нажмите, чтобы добавить:",
        reply_markup=kb_category(cat["id"], _cart_count(cart)),
    )


def _category_of_item(item_id: str) -> str | None:
    for c in MENU["categories"]:
        if any(it["id"] == item_id for it in c["items"]):
            return c["id"]
    return None


@router.callback_query(F.data.startswith("a:"))
async def cb_add(cb: CallbackQuery, state: FSMContext):
    item = f_find_item(cb.data[2:])
    if not item:
        await cb.answer(ERR_MENU, show_alert=True)
        return
    if not await _table(state):
        await cb.answer(ASK_TABLE_FIRST, show_alert=True)
        return
    cart = await _cart(state)
    if not _is_admin(cb.from_user.id) and _cart_count(cart) >= ORDER_MAX_ITEMS:
        await cb.answer(
            f"⚠️ В одном заказе не больше {ORDER_MAX_ITEMS} позиций. "
            "Отправьте этот заказ — и сделайте следующий.",
            show_alert=True,
        )
        return
    # лимит по остаткам: столько позиции можно набрать по рецепту
    limit = max_available(item["id"])
    in_cart = next((i["qty"] for i in cart if i["id"] == item["id"]), 0)
    if limit is not None and in_cart + 1 > limit:
        await cb.answer(
            f"⚠️ «{item['name']}»: по остаткам на складе можно не более {limit} шт. "
            "Ингредиентов на большее не хватает.",
            show_alert=True,
        )
        return
    found = next((i for i in cart if i["id"] == item["id"]), None)
    if found:
        found["qty"] += 1
    else:
        cart.append({"id": item["id"], "name": item["name"], "price": item["price"], "qty": 1})
    await state.update_data(cart=cart)
    qty = next(i["qty"] for i in cart if i["id"] == item["id"])
    await cb.answer(f"«{item['name']}» × {qty} — в корзине ({_cart_count(cart)} шт.)")
    # сразу обновляем счётчик на кнопке «🛒 Корзина (N)», не дожидаясь клика
    cat_id = _category_of_item(item["id"])
    if cat_id:
        try:
            await cb.message.edit_reply_markup(
                reply_markup=kb_category(cat_id, _cart_count(cart))
            )
        except TelegramBadRequest:
            pass


# ------------------------------------------------------- корзина --------------
async def _render_cart(cb: CallbackQuery, state: FSMContext) -> None:
    cart = await _cart(state)
    table = await _table(state)
    if not cart:
        await cb.message.edit_text(CART_EMPTY, reply_markup=kb_categories(0))
        return
    try:
        await cb.message.edit_text(cart_text(cart, table), reply_markup=kb_cart(cart))
    except TelegramBadRequest:
        pass  # сообщение не изменилось


def _cart_index(cart: list[dict], data: str) -> int | None:
    try:
        idx = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        return None
    return idx if 0 <= idx < len(cart) else None


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "cv")
async def cb_cart(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _render_cart(cb, state)


@router.callback_query(F.data.startswith("ci:"))
async def cb_cart_inc(cb: CallbackQuery, state: FSMContext):
    cart = await _cart(state)
    idx = _cart_index(cart, cb.data)
    if idx is None:
        await cb.answer(ERR_MENU, show_alert=True)
        return
    if not _is_admin(cb.from_user.id) and _cart_count(cart) >= ORDER_MAX_ITEMS:
        await cb.answer(f"⚠️ В одном заказе не больше {ORDER_MAX_ITEMS} позиций", show_alert=True)
        return
    limit = max_available(cart[idx]["id"]) if cart[idx].get("id") else None
    if limit is not None and cart[idx]["qty"] + 1 > limit:
        await cb.answer(
            f"⚠️ «{cart[idx]['name']}»: по остаткам можно не более {limit} шт.",
            show_alert=True,
        )
        return
    cart[idx]["qty"] += 1
    await state.update_data(cart=cart)
    await cb.answer(f"{cart[idx]['name']}: {cart[idx]['qty']} шт.")
    await _render_cart(cb, state)


@router.callback_query(F.data.startswith("cd:"))
async def cb_cart_dec(cb: CallbackQuery, state: FSMContext):
    cart = await _cart(state)
    idx = _cart_index(cart, cb.data)
    if idx is None:
        await cb.answer(ERR_MENU, show_alert=True)
        return
    cart[idx]["qty"] -= 1
    if cart[idx]["qty"] <= 0:
        cart.pop(idx)
    await state.update_data(cart=cart)
    await cb.answer("Уменьшено")
    await _render_cart(cb, state)


@router.callback_query(F.data.startswith("cr:"))
async def cb_cart_remove(cb: CallbackQuery, state: FSMContext):
    cart = await _cart(state)
    idx = _cart_index(cart, cb.data)
    if idx is None:
        await cb.answer(ERR_MENU, show_alert=True)
        return
    name = cart[idx]["name"]
    cart.pop(idx)
    await state.update_data(cart=cart)
    await cb.answer(f"«{name}» убрано")
    await _render_cart(cb, state)


@router.callback_query(F.data == "cx")
async def cb_cart_clear(cb: CallbackQuery, state: FSMContext):
    await state.update_data(cart=[])
    await cb.answer("Корзина очищена")
    await _render_cart(cb, state)


# -------------------------------------------------- оформление заказа ---------
@router.callback_query(F.data == "co")
async def cb_checkout(cb: CallbackQuery, state: FSMContext):
    cart = await _cart(state)
    if not cart:
        await cb.answer(CART_EMPTY, show_alert=True)
        return
    table = await _table(state)
    if not table:
        await cb.answer()
        await state.set_state(OrderFSM.table)
        await cb.message.edit_text(ASK_TABLE)
        return
    await cb.answer()
    await cb.message.edit_text(confirm_text(cart, table), reply_markup=kb_confirm())


@router.callback_query(F.data == "cb")
async def cb_back_to_cart(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _render_cart(cb, state)


_user_locks: dict[int, asyncio.Lock] = {}


def _user_lock(uid: int) -> asyncio.Lock:
    """Замок на гостя: двойной клик «Отправить» не создаст два заказа/брони."""
    lk = _user_locks.get(uid)
    if lk is None:
        lk = _user_locks[uid] = asyncio.Lock()
    return lk


def _order_limits_error(uid: int, cart: list[dict]) -> str | None:
    """Анти-спам лимиты заказа (админы из ADMIN_IDS не ограничены)."""
    if _is_admin(uid):
        return None
    if _cart_count(cart) > ORDER_MAX_ITEMS:
        return f"⚠️ В одном заказе не больше {ORDER_MAX_ITEMS} позиций — уменьшите количество."
    age = db_last_order_age_sec(uid)
    if age is not None and 0 <= age < ORDER_COOLDOWN_SEC:
        return (
            f"⏳ Следующий заказ можно отправить через {int(ORDER_COOLDOWN_SEC - age) + 1} сек. "
            "Это защита от случайных повторов."
        )
    return None


async def _submit_order(cb: CallbackQuery, state: FSMContext, kind: str) -> None:
    """Отправка заказа в канал — обычного (корзина cart) или по акции (pcart)."""
    key = "pcart" if kind == "promo" else "cart"
    uid = cb.from_user.id
    lock = _user_lock(uid)
    if lock.locked():
        await cb.answer("⏳ Уже отправляем заказ…")
        return
    async with lock:
        data = await state.get_data()
        cart = list(data.get(key) or [])
        table = data.get("table")
        render = _render_pcart if kind == "promo" else _render_cart
        if not cart:
            await cb.answer(CART_EMPTY, show_alert=True)
            return
        if not table:
            await cb.answer()
            await state.set_state(OrderFSM.table)
            await cb.message.edit_text(ASK_TABLE)
            return
        if kind == "promo":
            cart, bad = _promo_revalidate(cart)
            if bad:
                await state.update_data(pcart=cart)
                await cb.answer("⚠️ Часть акций сейчас недоступна", show_alert=True)
                await cb.message.answer("⚠️ <b>Убрали из заказа:</b>\n" + "\n".join(bad))
                await render(cb, state)
                return
        err = _order_limits_error(uid, cart)
        if err:
            await cb.answer(err, show_alert=True)
            return
        # финальная проверка остатков по всей корзине — заказ сверх склада не уйдёт
        problems = cart_stock_problems(cart)
        if problems:
            await cb.answer("⚠️ Не хватает на складе", show_alert=True)
            await cb.message.answer(
                "⚠️ <b>Не хватает на складе:</b>\n"
                + "\n".join(problems)
                + "\n\nУменьшите количество в корзине или выберите другой товар."
            )
            await render(cb, state)
            return

        await cb.answer("Отправляем…")
        total = _cart_total(cart)
        user = cb.from_user
        card = guest_card(uid)
        order_id = db_add_order(
            table_no=table,
            total=total,
            user_id=uid,
            username=user.username if user else None,
            phone=card["phone"] if card else data.get("phone"),
            kind=kind,
        )
        db_add_items(order_id, cart)

        order = db_get_order(order_id)
        try:
            sent = await cb.bot.send_message(
                CHANNEL_ID,
                order_text(order),
                reply_markup=kb_status(order_id),
            )
        except Exception:
            logger.exception("Не удалось отправить заказ #%s в канал (CHANNEL_ID=%s)",
                             order_id, CHANNEL_ID)
            db_delete_order(order_id)
            await cb.message.edit_text(
                "⚠️ Не удалось отправить заказ в канал. Сообщите администратору.",
                reply_markup=kb_pconfirm() if kind == "promo" else kb_confirm(),
            )
            return
        db_set_message_id(order_id, sent.message_id)
        await state.update_data(**{key: []})
        db_guest_touch(user)
        text = sent_text(order_id, table, total)
        if kind == "promo":
            text = "🎁 Заказ по акции.\n" + text
        elif card and LOYALTY_PERCENT:
            text += f"\n\n💎 После оплаты начислим кешбэк {LOYALTY_PERCENT}% баллами."
        await cb.message.edit_text(text)


@router.callback_query(F.data == "so")
async def cb_send_order(cb: CallbackQuery, state: FSMContext):
    await _submit_order(cb, state, "regular")


# ============================== БРОНИРОВАНИЕ ================================
# Кнопка «📅 Бронирование» → тип → дата → время → гости → подтверждение →
# сообщение в канал со статусами (редактируется тем же сообщением).

RSV_KINDS = {
    "table": "🪑 Стол",
    "bar": "🍸 Место за барной стойкой",
    "vip": "🔑 VIP-комната",
    "ps": "🎮 Аренда PlayStation",
}
# Сколько броней категории может идти ОДНОВРЕМЕННО («Ожидает» + «Подтверждена»).
# Занятость считается по интервалам времени: VIP 15:00–17:00 и 18:00–22:00 в один
# день — можно. После закрытия брони (оплата/отмена/не пришёл) место свободно.
RSV_CAPACITY = {"table": 8, "bar": 5, "vip": 1, "ps": 2}
RSV_CLOSE_H = 3                      # закрытие в 03:00 (работаем 15:00–03:00)
RSV_BUFFER_MIN = {"vip": 30}         # уборка VIP-комнаты между бронями, минут
# сколько стол считается занятым: «1–2 часа» → 2 ч, «2–3 часа» → 3 ч, «3+» → до закрытия
RSV_DUR_OCCUPY_MIN = {"1-2": 120, "2-3": 180, "3+": None}
# сколько минут должно оставаться до закрытия, чтобы вариант стола показывался
RSV_DUR_NEED_MIN = {"1-2": 60, "2-3": 120, "3+": 180}
RSV_BAR_OCCUPY_MIN = 180             # место за стойкой (без часов) — расчётно 3 ч
RSV_BAR_NEED_MIN = 60                # за стойку — не позже чем за час до закрытия
# VIP/PlayStation: пакет (2/4/6 ч) должен целиком закончиться до закрытия
RSV_DURATIONS = {
    # 🪑 Стол
    "1-2": "1–2 часа",
    "2-3": "2–3 часа",
    "3+": "3 и более",
    # 🔑 VIP-комната
    "vip-2": "2 часа - 1 500 ₽",
    "vip-4": "4 часа - 2 500 ₽",
    "vip-6": "6 часов - 3 000 ₽",
    # 🎮 PlayStation
    "ps-2": "2 часа - 400 ₽",
    "ps-4": "4 часа - 600 ₽",
    "ps-6": "6 часов - 700 ₽",
}
# какие длительности показывать каждому виду брони
RSV_DUR_BY_KIND = {
    "table": ["1-2", "2-3", "3+"],
    "bar": ["1-2", "2-3", "3+"],
    "vip": ["vip-2", "vip-4", "vip-6"],
    "ps": ["ps-2", "ps-4", "ps-6"],
}
# подписи под кнопками длительности
RSV_DUR_CAPTIONS = {
    "vip": "При заказе от 6000 ₽ аренда - бессрочно и бесплатно!",
    "ps": "При заказе от 3500 ₽ аренда - бессрочно и бесплатно!",
}
# --- почасовая аренда VIP/PlayStation: правка ±15 мин администратором ---
RSV_BOOKED_MIN = {
    "vip-2": 120, "vip-4": 240, "vip-6": 360,
    "ps-2": 120, "ps-4": 240, "ps-6": 360,
}
RSV_BASE_PRICE = {
    "vip-2": 1500, "vip-4": 2500, "vip-6": 3000,
    "ps-2": 400, "ps-4": 600, "ps-6": 700,
}
RSV_STEP_MIN = 15
RSV_STEP_PRICE = {"vip": 188, "ps": 50}   # ₽ за 15 мин (от тарифа пакета 2 ч)
RSV_ADJ_KINDS = ("vip", "ps")
RSV_MIN_MIN = 15
RSV_DAYS = 30          # на сколько дней вперёд можно бронировать (1 месяц)
RSV_LEAD_MIN = 10      # минимальный запас до времени брони, минут
WDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _fmt_minutes(m: int) -> str:
    h, mi = divmod(int(m), 60)
    if h and mi:
        return f"{h} ч {mi} мин"
    if h:
        return f"{h} ч"
    return f"{mi} мин"


def _rsv_actual_min(r: dict) -> int:
    if r.get("actual_min"):
        return int(r["actual_min"])
    return RSV_BOOKED_MIN.get(r.get("duration") or "", 0)


def _rsv_price(r: dict) -> int | None:
    """Цена брони по факту времени. None — виды без почасовой оплаты (стол, бар)."""
    code = r.get("duration") or ""
    base = RSV_BASE_PRICE.get(code)
    if base is None or r.get("kind") not in RSV_ADJ_KINDS:
        return None
    steps = (_rsv_actual_min(r) - RSV_BOOKED_MIN[code]) // RSV_STEP_MIN
    return max(0, base + steps * RSV_STEP_PRICE[r["kind"]])


def _slots() -> list[str]:
    """Слоты времени: 15:00 → 02:30 с шагом 30 минут (работаем 15:00–03:00)."""
    out: list[str] = []
    for h in (15, 16, 17, 18, 19, 20, 21, 22, 23, 0, 1, 2):
        out.append(f"{h:02d}:00")
        out.append(f"{h:02d}:30")
    return out


def _arrival(date_s: str, time_s: str) -> datetime:
    """Фактическое время прихода: ночные слоты (00:00–03:00) — это утро
    СЛЕДУЮЩЕГО дня после выбранной даты (работаем до 03:00)."""
    d = datetime.strptime(date_s, "%Y-%m-%d").date()
    h, m = map(int, time_s.split(":"))
    if h < 15:
        d = d + timedelta(days=1)
    return datetime(d.year, d.month, d.day, h, m)


def _wd(d) -> str:
    return WDAYS[d.weekday()]


def _rsv_when_text(date_s: str, time_s: str) -> str:
    d0 = datetime.strptime(date_s, "%Y-%m-%d").date()
    d1 = _arrival(date_s, time_s).date()
    if d1 != d0:
        return f"{_wd(d0)} {d0:%d.%m} → {_wd(d1)} {d1:%d.%m} · {time_s}"
    return f"{_wd(d0)} {d0:%d.%m} · {time_s}"


def _guests_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "гость"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "гостя"
    return "гостей"


def _biz_today():
    """Текущий рабочий день: до 03:00 ещё идёт вчерашняя смена (15:00–03:00).
    В 01:00 вторника это понедельник — его ночные слоты ещё можно бронировать."""
    return (_now_dt() - timedelta(hours=RSV_CLOSE_H)).date()


def _date_label(d) -> str:
    today = _now_dt().date()
    if d == _biz_today() and d != today:
        return f"🌙 {d:%d.%m} (эта ночь)"
    if d == today:
        return f"📅 {d:%d.%m} (сегодня)"
    if d == today + timedelta(days=1):
        return f"📅 {d:%d.%m} (завтра)"
    return f"📅 {d:%d.%m} {_wd(d)}"


def _guest_name(user) -> str:
    if not user:
        return "Гость"
    name = user.first_name or "Гость"
    if user.username:
        return f"{name} (@{user.username})"
    return name


def rsv_text(r: dict) -> str:
    lines = [
        f"📅 <b>Бронь #{r['id']}</b>",
        RSV_KINDS.get(r["kind"], r["kind"]),
        f"📆 {_rsv_when_text(r['date'], r['time'])}",
        f"👥 {r['guests']} {_guests_word(r['guests'])}",
    ]
    if r.get("duration"):
        lines.append(f"⏱ {RSV_DURATIONS.get(r['duration'], r['duration'])}")
    try:
        t0, t1 = rsv_interval(r["kind"], r["date"], r["time"], r.get("duration") or "",
                              r.get("actual_min"))
        approx = " (расчётно)" if r["kind"] not in RSV_ADJ_KINDS else ""
        lines.append(f"🕒 {t0:%H:%M}–{t1:%H:%M}{approx}")
    except (ValueError, TypeError, KeyError):
        pass
    price = _rsv_price(r)
    if price is not None:
        if r["status"] == "paid":
            lines.append(
                f"💳 Оплачено: {_fmt_money(price)}"
                f" · {PAY_METHODS.get(r.get('pay_method') or '', '—')}"
            )
        else:
            fact = _fmt_minutes(_rsv_actual_min(r))
            booked = RSV_BOOKED_MIN.get(r.get("duration") or "")
            if booked and _rsv_actual_min(r) != booked:
                lines.append(f"🧾 К оплате: {_fmt_money(price)} (факт {fact})")
            else:
                lines.append(f"🧾 К оплате: {_fmt_money(price)}")
    name = r["guest_name"]
    if r.get("username") and "(@" not in str(name):
        name = f"{name} (@{r['username']})"
    lines += [f"👤 {esc(name)}"]
    if r.get("phone"):
        lines.append(f"📞 {r['phone']}")
    lines += [""]
    status = RSV_STATUSES.get(r["status"], r["status"])
    ts = (r.get("updated_at") or "")[11:16]
    lines.append(f"Статус: {status} · {ts}")
    return "\n".join(lines)


def _rsv_notify_text(r: dict) -> str:
    """Сообщение гостю о смене статуса его брони."""
    base = (
        f"📅 Бронь <b>#{r['id']}</b>\n"
        f"{RSV_KINDS.get(r['kind'], r['kind'])} · "
        f"{_rsv_when_text(r['date'], r['time'])}"
    )
    if r["status"] == "confirmed":
        return (
            f"✅ <b>Ваша бронь подтверждена!</b>\n\n{base}\n\n"
            "Ждём вас! Если планы изменятся — сообщите заранее."
        )
    if r["status"] == "paid":
        return (
            f"💳 <b>Бронь #{r['id']} оплачена</b>\n\n{base}\n\n"
            "Спасибо, что выбрали нас! Ждём снова ⚡"
        )
    if r["status"] == "cancelled":
        return (
            f"❌ <b>Ваша бронь отменена</b>\n\n{base}\n\n"
            "Если что-то изменилось — забронируйте снова через /start."
        )
    if r["status"] == "no_show":
        return (
            f"🚫 <b>Вы не пришли на бронь</b>\n\n{base}\n\n"
            "Если захотите снова — забронируйте через /start."
        )
    return (
        f"⏳ <b>Ваша бронь #{r['id']}</b> снова в статусе «Ожидает».\n"
        "Администратор вернётся к ней."
    )


# --- клавиатуры брони ---
def kb_rsv_kind() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(RSV_KINDS["table"], "rsv:kind:table")],
            [_btn(RSV_KINDS["bar"], "rsv:kind:bar")],
            [_btn(RSV_KINDS["vip"], "rsv:kind:vip")],
            [_btn(RSV_KINDS["ps"], "rsv:kind:ps")],
            [_btn("🏠 В начало", "go:home")],
        ]
    )


def kb_rsv_dates() -> InlineKeyboardMarkup:
    today = _biz_today()
    dates = [today + timedelta(days=i) for i in range(RSV_DAYS)]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(dates), 3):
        row = [_btn(_date_label(d), f"rsv:date:{d.isoformat()}") for d in dates[i : i + 3]]
        rows.append(row)
    rows.append([_btn("🔙 Назад", "rsv:back:kind")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_rsv_times(avail: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(avail), 4):
        rows.append([_btn(s, f"rsv:time:{s}") for s in avail[i : i + 4]])
    rows.append([_btn("🔙 Другая дата", "rsv:back:date")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_rsv_guests(kind: str | None = None) -> InlineKeyboardMarkup:
    if kind == "bar":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [_btn(str(n), f"rsv:g:{n}") for n in range(1, 6)],
                [_btn("🔙 Назад", "rsv:back:time")],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(str(n), f"rsv:g:{n}") for n in range(1, 6)],
            [_btn(str(n), f"rsv:g:{n}") for n in range(6, 11)],
            [_btn("🔙 Назад", "rsv:back:time")],
        ]
    )


def kb_rsv_duration(kind: str = "table", codes: list[str] | None = None) -> InlineKeyboardMarkup:
    """codes — доступные варианты (помещаются до закрытия и свободны)."""
    if codes is None:
        codes = RSV_DUR_BY_KIND.get(kind, RSV_DUR_BY_KIND["table"])
    btns = [_btn(RSV_DURATIONS[c], f"rsv:dur:{c}") for c in codes]
    rows = [btns[i : i + 2] for i in range(0, len(btns), 2)]
    rows.append([_btn("🔙 Назад", "rsv:back:name")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _rsv_dur_screen(rsv: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Экран выбора длительности: только варианты, которые помещаются до
    закрытия и свободны. Если всё заняли — предлагаем другое время."""
    kind = rsv.get("kind") or "table"
    codes = rsv_options(kind, rsv["date"], rsv["time"])
    if not codes:
        return (
            "😢 Пока вы заполняли форму, это время заняли.\nВыберите другое время 👇",
            InlineKeyboardMarkup(inline_keyboard=[[_btn("🔙 Другое время", "rsv:back:time")]]),
        )
    text = _rsv_dur_text(rsv)
    if len(codes) < len(RSV_DUR_BY_KIND.get(kind, [])):
        text += "\n\nℹ️ Показаны варианты, которые свободны и заканчиваются до закрытия (03:00)."
    return text, kb_rsv_duration(kind, codes)


def kb_rsv_name() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn("🔙 Назад", "rsv:back:guests")]]
    )


def kb_rsv_confirm(rsv: dict | None = None) -> InlineKeyboardMarkup:
    # у барной стойки часов нет — «Назад» возвращает к вводу имени
    back = "rsv:back:name" if (rsv or {}).get("kind") == "bar" else "rsv:back:dur"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✅ Отправить бронь", "rsv:ok")],
            [
                _btn("🔙 Назад", back),
                _btn("⏹ Отменить", "rsv:no"),
            ],
        ]
    )


def kb_rsv_status(rid: int, r: dict | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if r and r.get("kind") in RSV_ADJ_KINDS and r.get("status") in ("new", "confirmed"):
        rows.append([
            _btn("➖ 15 мин", f"rsa:{rid}:-15"),
            _btn("➕ 15 мин", f"rsa:{rid}:+15"),
        ])
        rows.append([_btn("💳 Закрыть чек", f"rsp:{rid}")])
    if r and r.get("status") == "paid":
        rows.append([_btn("↩️ Оформить возврат", f"rs:{rid}:cancelled")])
    else:
        rows.append([
            _btn(RSV_STATUSES["confirmed"], f"rs:{rid}:confirmed"),
            _btn(RSV_STATUSES["cancelled"], f"rs:{rid}:cancelled"),
        ])
        rows.append([
            _btn(RSV_STATUSES["no_show"], f"rs:{rid}:no_show"),
            _btn(RSV_STATUSES["new"], f"rs:{rid}:new"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- экраны ---
async def _rsv_show_dates(target, edit: bool = False):
    text = "📅 <b>Бронирование</b>\n\nНа какую дату?"
    kb = kb_rsv_dates()
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    else:
        await target.answer(text, reply_markup=kb)


_RSV_TIME_HINTS = {
    "vip": "Показано время, когда комната свободна и аренда заканчивается до 03:00 "
           "(между бронями — 30 мин на уборку).",
    "ps": "Показано время, когда есть свободная приставка и аренда заканчивается до 03:00.",
}


async def _rsv_show_times(target, date_s: str, kind: str | None = None, note: str = ""):
    if kind:
        avail = rsv_slots_available(kind, date_s)
    else:
        now = _now_dt() + timedelta(minutes=RSV_LEAD_MIN)
        avail = [s for s in _slots() if _arrival(date_s, s) > now]
    d_obj = datetime.strptime(date_s, "%Y-%m-%d").date()
    text = (
        f"{note}⏰ Выберите время — {_date_label(d_obj)}\n"
        "Кальянная работает <b>с 15:00 до 03:00</b>"
    )
    if kind in _RSV_TIME_HINTS and avail:
        text += f"\nℹ️ {_RSV_TIME_HINTS[kind]}"
    if not avail:
        text += "\n\n😢 На эту дату свободного времени уже нет — выберите другую."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[_btn("🔙 Другая дата", "rsv:back:date")]]
        )
    else:
        kb = kb_rsv_times(avail)
    try:
        await target.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


def _rsv_summary_lines(rsv: dict) -> list[str]:
    lines = []
    if rsv.get("kind"):
        lines.append(f"📌 {RSV_KINDS.get(rsv['kind'], rsv['kind'])}")
    if rsv.get("date") and rsv.get("time"):
        lines.append(f"📆 {_rsv_when_text(rsv['date'], rsv['time'])}")
    if rsv.get("guests"):
        lines.append(f"👥 {rsv['guests']} {_guests_word(rsv['guests'])}")
    return lines


def _rsv_guests_text(rsv: dict) -> str:
    return "\n".join(["👥 Сколько гостей?", ""] + _rsv_summary_lines(rsv))


def _rsv_dur_text(rsv: dict) -> str:
    lines = ["⏱ На сколько часов бронируем?", ""] + _rsv_summary_lines(rsv)
    cap = RSV_DUR_CAPTIONS.get(rsv.get("kind") or "")
    if cap:
        lines += ["", f"ℹ️ {cap}"]
    return "\n".join(lines)


def _rsv_name_text(rsv: dict) -> str:
    return "\n".join(
        ["✍️ Имя, на кого бронь?", ""]
        + _rsv_summary_lines(rsv)
        + ["", "Напишите имя обычным сообщением ↩️"]
    )


def _rsv_confirm_text(rsv: dict, name: str) -> str:
    lines = [
        "✅ <b>Подтверждение брони</b>",
        "",
        f"📌 {RSV_KINDS.get(rsv.get('kind'), '—')}",
        f"📆 {_rsv_when_text(rsv['date'], rsv['time'])}",
        f"👥 {rsv['guests']} {_guests_word(rsv['guests'])}",
    ]
    if rsv.get("duration"):
        lines.append(f"⏱ {RSV_DURATIONS.get(rsv['duration'], '—')}")
        if rsv.get("kind") in RSV_ADJ_KINDS:
            try:
                t0, t1 = rsv_interval(rsv["kind"], rsv["date"], rsv["time"], rsv["duration"])
                lines.append(f"🕒 {t0:%H:%M}–{t1:%H:%M}")
            except (ValueError, KeyError):
                pass
    lines += [
        f"👤 {esc(name)}",
        "",
        "Отправить бронь администратору?",
    ]
    return "\n".join(lines)


# --- хендлеры ---
@router.message(F.text == BTN_BOOK)
async def on_btn_book(message: Message, state: FSMContext):
    await _begin_flow("rsv", message, state)


@router.callback_query(F.data.startswith("rsv:kind:"))
async def rsv_kind(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":", 2)[2]
    if kind not in RSV_KINDS:
        await cb.answer(ERR_MENU, show_alert=True)
        return
    if not _is_admin(cb.from_user.id) and db_rsv_user_active(cb.from_user.id) >= RSV_MAX_ACTIVE_PER_USER:
        await cb.answer(_RSV_LIMIT_TEXT, show_alert=True)
        return
    await state.update_data(rsv={"kind": kind})
    await cb.answer()
    await _rsv_show_dates(cb.message, edit=True)


@router.callback_query(F.data.startswith("rsv:date:"))
async def rsv_date(cb: CallbackQuery, state: FSMContext):
    date_s = cb.data.split(":", 2)[2]
    try:
        d_obj = datetime.strptime(date_s, "%Y-%m-%d").date()
    except ValueError:
        await cb.answer("Некорректная дата", show_alert=True)
        return
    if not _biz_today() <= d_obj < _biz_today() + timedelta(days=RSV_DAYS):
        await cb.answer("Эта дата недоступна для брони", show_alert=True)
        return
    data = await state.get_data()
    rsv = dict(data.get("rsv") or {})
    if rsv.get("kind") is None:
        await cb.answer("Начните бронирование заново", show_alert=True)
        await cb.message.edit_text(
            "📅 <b>Бронирование</b>\n\nЧто будем бронировать?",
            reply_markup=kb_rsv_kind(),
        )
        return
    # свободное время на эту дату (интервалы, закрытие 03:00, уже прошедшее)
    if rsv_capacity_full(rsv["kind"], date_s):
        await cb.answer(
            f"На {_date_label(d_obj)[2:]} для «{RSV_KINDS[rsv['kind']]}» "
            "свободного времени нет. Выберите другую дату",
            show_alert=True,
        )
        return
    rsv["date"] = date_s
    await state.update_data(rsv=rsv)
    await cb.answer()
    await _rsv_show_times(cb.message, date_s, rsv["kind"])


@router.callback_query(F.data.startswith("rsv:time:"))
async def rsv_time(cb: CallbackQuery, state: FSMContext):
    time_s = cb.data.split(":", 2)[2]
    if time_s not in _slots():
        await cb.answer("Некорректное время", show_alert=True)
        return
    data = await state.get_data()
    rsv = dict(data.get("rsv") or {})
    if not rsv.get("kind") or not rsv.get("date"):
        await cb.answer("Начните бронирование заново", show_alert=True)
        await cb.message.edit_text(
            "📅 <b>Бронирование</b>\n\nЧто будем бронировать?",
            reply_markup=kb_rsv_kind(),
        )
        return
    if time_s not in rsv_slots_available(rsv["kind"], rsv["date"]):
        await cb.answer("Это время уже занято или недоступно — выберите другое", show_alert=True)
        await _rsv_show_times(cb.message, rsv["date"], rsv["kind"])
        return
    rsv["time"] = time_s
    rsv.pop("duration", None)
    await state.update_data(rsv=rsv)
    await cb.answer()
    await cb.message.edit_text(
        _rsv_guests_text(rsv),
        reply_markup=kb_rsv_guests(rsv.get("kind")),
    )


@router.callback_query(F.data.startswith("rsv:g:"))
async def rsv_guests(cb: CallbackQuery, state: FSMContext):
    try:
        n = int(cb.data.split(":", 2)[2])
    except ValueError:
        n = 0
    data = await state.get_data()
    rsv = dict(data.get("rsv") or {})
    gmax = 5 if rsv.get("kind") == "bar" else 10
    if not 1 <= n <= gmax:
        await cb.answer(
            "За барной стойкой — от 1 до 5 мест" if gmax == 5 else "От 1 до 10 гостей",
            show_alert=True,
        )
        return
    if not (rsv.get("kind") and rsv.get("date") and rsv.get("time")):
        await cb.answer("Начните бронирование заново", show_alert=True)
        await cb.message.edit_text(
            "📅 <b>Бронирование</b>\n\nЧто будем бронировать?",
            reply_markup=kb_rsv_kind(),
        )
        return
    rsv["guests"] = n
    await state.update_data(rsv=rsv)
    # следующий шаг — имя, на кого бронь
    await state.set_state(OrderFSM.rsv_name)
    await cb.answer()
    try:
        await cb.message.edit_text(_rsv_name_text(rsv), reply_markup=kb_rsv_name())
        await state.update_data(rsv_name_msg=cb.message.message_id)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("rsv:dur:"))
async def rsv_duration(cb: CallbackQuery, state: FSMContext):
    code = cb.data.split(":", 2)[2]
    if code not in RSV_DURATIONS:
        await cb.answer("Некорректная длительность", show_alert=True)
        return
    data = await state.get_data()
    rsv = dict(data.get("rsv") or {})
    if not (
        rsv.get("kind")
        and rsv.get("date")
        and rsv.get("time")
        and rsv.get("guests")
        and rsv.get("name")
    ):
        await cb.answer("Начните бронирование заново", show_alert=True)
        await cb.message.edit_text(
            "📅 <b>Бронирование</b>\n\nЧто будем бронировать?",
            reply_markup=kb_rsv_kind(),
        )
        return
    allowed = RSV_DUR_BY_KIND.get(rsv.get("kind") or "", [])
    if code not in allowed:
        await cb.answer("Выберите длительность из списка", show_alert=True)
        return
    if code not in rsv_options(rsv["kind"], rsv["date"], rsv["time"]):
        await cb.answer(
            "Этот вариант недоступен: не помещается до закрытия (03:00) или уже занят",
            show_alert=True,
        )
        text, kb = _rsv_dur_screen(rsv)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        return
    rsv["duration"] = code
    await state.update_data(rsv=rsv)
    await state.set_state(None)
    await cb.answer()
    nm = rsv["name"]
    if cb.from_user and cb.from_user.username and "(@" not in nm:
        nm += f" (@{cb.from_user.username})"
    await cb.message.edit_text(
        _rsv_confirm_text(rsv, nm),
        reply_markup=kb_rsv_confirm(rsv),
    )


_RSV_LIMIT_TEXT = (
    f"⚠️ У вас уже {RSV_MAX_ACTIVE_PER_USER} активные брони — это максимум. "
    "Дождитесь визита или попросите администратора отменить лишнюю."
)


@router.callback_query(F.data == "rsv:ok")
async def rsv_send(cb: CallbackQuery, state: FSMContext):
    lock = _user_lock(cb.from_user.id)
    if lock.locked():
        await cb.answer("⏳ Уже отправляем бронь…")
        return
    async with lock:
        await _rsv_send_locked(cb, state)


async def _rsv_send_locked(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    rsv = dict(data.get("rsv") or {})
    need_dur = rsv.get("kind") != "bar"  # барная стойка — место без часов
    if not (
        rsv.get("kind")
        and rsv.get("date")
        and rsv.get("time")
        and rsv.get("guests")
        and rsv.get("name")
        and (rsv.get("duration") or not need_dur)
    ):
        await cb.answer("Данные брони неполные — начните заново", show_alert=True)
        await cb.message.edit_text(
            "📅 <b>Бронирование</b>\n\nЧто будем бронировать?",
            reply_markup=kb_rsv_kind(),
        )
        return

    user = cb.from_user
    card = guest_card(user.id)
    # проверка лимитов и запись — одной операцией (без гонки за последнее место)
    rid, why = db_add_reservation_checked(
        None if _is_admin(user.id) else RSV_MAX_ACTIVE_PER_USER,
        kind=rsv["kind"],
        date_s=rsv["date"],
        time_s=rsv["time"],
        guests=rsv["guests"],
        duration=rsv.get("duration") or "",
        guest_name=rsv["name"],
        user_id=user.id,
        username=user.username,
        phone=card["phone"] if card else data.get("phone"),
    )
    if rid is None and why == "user_limit":
        await state.update_data(rsv={})
        await cb.answer(_RSV_LIMIT_TEXT, show_alert=True)
        try:
            await cb.message.edit_text("⚠️ <b>Бронь не отправлена</b>\n\n" + _RSV_LIMIT_TEXT)
        except TelegramBadRequest:
            pass
        return
    if rid is None:
        # пока заполняли форму, это время заняли (или оно уже прошло)
        rsv.pop("time", None)
        rsv.pop("duration", None)
        await state.update_data(rsv=rsv)
        await cb.answer()
        await _rsv_show_times(
            cb.message, rsv["date"], rsv["kind"],
            note="⚠️ <b>Бронь не отправлена</b> — пока заполняли форму, это время "
                 "заняли или оно уже прошло. Выберите другое 👇\n\n",
        )
        return

    await cb.answer("Отправляем…")
    r = db_get_reservation(rid)
    try:
        sent = await cb.bot.send_message(
            CHANNEL_ID,
            rsv_text(r),
            reply_markup=kb_rsv_status(rid, r),
        )
    except Exception:
        logger.exception(
            "Не удалось отправить бронь #%s в канал (CHANNEL_ID=%s)", rid, CHANNEL_ID
        )
        db_delete_reservation(rid)
        # cb.answer уже был («Отправляем…») — второй ответ Telegram отклонит,
        # поэтому сообщаем об ошибке в самом сообщении (кнопки остаются)
        try:
            await cb.message.edit_text(
                "⚠️ Не удалось отправить бронь администратору. "
                "Попробуйте ещё раз или сообщите персоналу.",
                reply_markup=kb_rsv_confirm(rsv),
            )
        except TelegramBadRequest:
            pass
        return
    db_set_rsv_message_id(rid, sent.message_id)
    await state.update_data(rsv={})
    await cb.message.edit_text(
        f"✅ Бронь <b>#{rid}</b> отправлена!\n\n"
        f"📌 {RSV_KINDS.get(rsv['kind'], '')}\n"
        f"📆 {_rsv_when_text(rsv['date'], rsv['time'])}\n"
        f"👥 {rsv['guests']} {_guests_word(rsv['guests'])}\n"
        + (f"⏱ {RSV_DURATIONS.get(rsv['duration'], '')}\n" if rsv.get("duration") else "")
        + "\n"
        "Администратор подтвердит бронь — статус сообщим здесь. Спасибо!"
    )


@router.callback_query(F.data.startswith("rsv:back:"))
async def rsv_back(cb: CallbackQuery, state: FSMContext):
    step = cb.data.split(":", 2)[2]
    data = await state.get_data()
    rsv = dict(data.get("rsv") or {})
    await cb.answer()
    await state.set_state(None)
    if step == "kind":
        await cb.message.edit_text(
            "📅 <b>Бронирование</b>\n\nЧто будем бронировать?",
            reply_markup=kb_rsv_kind(),
        )
    elif step == "date" and rsv.get("kind"):
        await _rsv_show_dates(cb.message, edit=True)
    elif step == "time" and rsv.get("date"):
        await _rsv_show_times(cb.message, rsv["date"], rsv.get("kind"))
    elif step == "guests" and rsv.get("time"):
        await cb.message.edit_text(
            _rsv_guests_text(rsv),
            reply_markup=kb_rsv_guests(rsv.get("kind")),
        )
    elif step == "name" and rsv.get("guests"):
        await state.set_state(OrderFSM.rsv_name)
        try:
            await cb.message.edit_text(_rsv_name_text(rsv), reply_markup=kb_rsv_name())
            await state.update_data(rsv_name_msg=cb.message.message_id)
        except TelegramBadRequest:
            pass
    elif step == "dur" and rsv.get("name") and rsv.get("time"):
        text, kb = _rsv_dur_screen(rsv)
        await cb.message.edit_text(text, reply_markup=kb)
    else:
        await cb.message.edit_text(
            "📅 <b>Бронирование</b>\n\nЧто будем бронировать?",
            reply_markup=kb_rsv_kind(),
        )


@router.callback_query(F.data == "rsv:no")
async def rsv_cancel(cb: CallbackQuery, state: FSMContext):
    await state.update_data(rsv={})
    await state.set_state(None)
    await cb.answer("Отменено")
    try:
        await cb.message.edit_text("Бронирование отменено ✅")
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("rs:"))
async def cb_rsv_status(cb: CallbackQuery):
    try:
        _, rid_raw, code = cb.data.split(":")
        rid = int(rid_raw)
    except (ValueError, AttributeError):
        await cb.answer("Некорректный запрос", show_alert=True)
        return

    if code == "paid":
        await cb.answer("Оплату оформляйте кнопкой «💳 Закрыть чек»", show_alert=True)
        return
    if code not in RSV_STATUSES:
        await cb.answer("Неизвестный статус", show_alert=True)
        return

    if not await _staff_guard(cb, "⚠️ Статус меняют только администраторы канала"):
        return

    r = db_get_reservation(rid)
    if not r:
        await cb.answer("Бронь не найдена", show_alert=True)
        return

    label = RSV_STATUSES[code]
    if r["status"] == code:
        await cb.answer(f"Уже: {label}")
        return

    # Финальные статусы «заперты»: из «Отменена» / «Не пришёл»
    # активные кнопки не работают — вернуть бронь можно только через «Ожидает».
    if r["status"] in ("cancelled", "no_show") and code != "new":
        await cb.answer(
            f"🔒 Бронь: {RSV_STATUSES[r['status']]} — активные статусы не ставятся. "
            "Возврат — только «🟡 Ожидает»",
            show_alert=True,
        )
        return

    if r["status"] == "paid" and code != "cancelled":
        await cb.answer(
            "🔒 Бронь оплачена — активные статусы не ставятся. "
            "Возврат — только «❌ Отменена»",
            show_alert=True,
        )
        return

    # возврат отменённой брони в работу — предупредим, если её время уже заняли
    warn = ""
    if r["status"] in ("cancelled", "no_show") and code in ("new", "confirmed"):
        try:
            if not _rsv_free_in(db_rsv_active_rows(r["kind"], r["date"], exclude_id=rid),
                                r["kind"], r["date"], r["time"], r.get("duration") or "",
                                r.get("actual_min")):
                warn = "\n⚠️ На это время мест уже нет — проверьте, не будет ли накладки"
        except (ValueError, TypeError):
            pass

    db_set_rsv_status(rid, code)
    r = db_get_reservation(rid)
    try:
        await cb.message.edit_text(rsv_text(r), reply_markup=kb_rsv_status(rid, r))
    except Exception:
        logger.exception("Не удалось отредактировать бронь #%s", rid)

    # --- уведомление гостю о смене статуса ---
    if r.get("user_id"):
        try:
            await cb.bot.send_message(r["user_id"], _rsv_notify_text(r))
        except Exception:
            logger.info(
                "Не удалось уведомить гостя брони #%s (user_id=%s) — возможно, "
                "он не начинал диалог с ботом или заблокировал его",
                rid,
                r.get("user_id"),
            )
    await cb.answer(f"{label}{warn}", show_alert=bool(warn))


@router.callback_query(F.data.startswith("rsa:"))
async def cb_rsv_adjust(cb: CallbackQuery):
    """Правка фактического времени аренды VIP/PlayStation: ±15 минут."""
    try:
        _, rid_raw, sign = cb.data.split(":")
        rid = int(rid_raw)
    except (ValueError, AttributeError):
        await cb.answer("Некорректный запрос", show_alert=True)
        return
    if sign not in ("+15", "-15"):
        await cb.answer("Некорректная правка", show_alert=True)
        return
    if not await _staff_guard(cb, "⚠️ Правку выполняют только администраторы канала"):
        return
    r = db_get_reservation(rid)
    if not r:
        await cb.answer("Бронь не найдена", show_alert=True)
        return
    if r["kind"] not in RSV_ADJ_KINDS or r["status"] not in ("new", "confirmed"):
        await cb.answer(
            "🔒 Правка доступна только для активной аренды VIP/PlayStation",
            show_alert=True,
        )
        return
    if not RSV_BOOKED_MIN.get(r.get("duration") or ""):
        await cb.answer("У этой брони нет почасовой оплаты", show_alert=True)
        return
    actual = max(
        RSV_MIN_MIN,
        _rsv_actual_min(r) + (RSV_STEP_MIN if sign == "+15" else -RSV_STEP_MIN),
    )
    rr = dict(r)
    rr["actual_min"] = actual
    price = _rsv_price(rr) or 0
    # продление: предупредим о закрытии и о следующей брони (не блокируем)
    warn = ""
    if sign == "+15":
        try:
            _, t1 = rsv_interval(r["kind"], r["date"], r["time"], r.get("duration") or "", actual)
            if t1 > _rsv_close_dt(r["date"]):
                warn += "\n⚠️ Аренда заканчивается после закрытия (03:00)"
            if not _rsv_free_in(db_rsv_active_rows(r["kind"], r["date"], exclude_id=rid),
                                r["kind"], r["date"], r["time"], r.get("duration") or "", actual):
                warn += "\n⚠️ Пересекается со следующей бронью"
        except (ValueError, TypeError):
            pass
    db_set_rsv_actual(rid, actual, price)
    r = db_get_reservation(rid)
    try:
        await cb.message.edit_text(rsv_text(r), reply_markup=kb_rsv_status(rid, r))
    except Exception:
        logger.exception("Не удалось обновить бронь #%s после правки", rid)
    await cb.answer(f"Факт: {_fmt_minutes(actual)} · к оплате {_fmt_money(price)}{warn}",
                    show_alert=bool(warn))


@router.callback_query(F.data.startswith("rsp:"))
async def cb_rsv_pay(cb: CallbackQuery):
    """Закрытие чека аренды: способ оплаты → статус «Оплачен»."""
    parts = cb.data.split(":")
    try:
        rid = int(parts[1])
    except (ValueError, IndexError):
        await cb.answer("Некорректный запрос", show_alert=True)
        return
    if not await _staff_guard(cb, "⚠️ Оплату принимают только администраторы канала"):
        return
    r = db_get_reservation(rid)
    if not r:
        await cb.answer("Бронь не найдена", show_alert=True)
        return
    if r["kind"] not in RSV_ADJ_KINDS or r["status"] not in ("new", "confirmed"):
        await cb.answer(
            "🔒 Закрытие чека доступно только для активной аренды VIP/PlayStation",
            show_alert=True,
        )
        return
    if len(parts) == 2:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _btn(PAY_BUTTONS["cash"], f"rsp:{rid}:cash"),
                    _btn(PAY_BUTTONS["card"], f"rsp:{rid}:card"),
                ],
                [_btn(PAY_BUTTONS["transfer"], f"rsp:{rid}:transfer")],
                [_btn("🔙 Назад", f"rsp:{rid}:back")],
            ]
        )
        try:
            await cb.message.edit_reply_markup(reply_markup=kb)
        except TelegramBadRequest:
            pass
        await cb.answer(f"К оплате: {_fmt_money(_rsv_price(r) or 0)} — способ?")
        return
    action = parts[2]
    if action == "back":
        try:
            await cb.message.edit_reply_markup(reply_markup=kb_rsv_status(rid, r))
        except TelegramBadRequest:
            pass
        await cb.answer("Отменено")
        return
    if action not in PAY_METHODS:
        await cb.answer("Неизвестный способ", show_alert=True)
        return
    price = _rsv_price(r) or 0
    actual = _rsv_actual_min(r)
    db_set_rsv_paid(rid, action, actual, price)
    r = db_get_reservation(rid)
    try:
        await cb.message.edit_text(rsv_text(r), reply_markup=kb_rsv_status(rid, r))
    except Exception:
        logger.exception("Не удалось обновить бронь #%s после оплаты", rid)
    if r.get("user_id"):
        try:
            await cb.bot.send_message(r["user_id"], _rsv_notify_text(r))
        except Exception:
            logger.info("Не удалось уведомить гостя брони #%s об оплате", rid)
    await cb.answer(f"Оплачено: {_fmt_money(price)} ({PAY_METHODS[action]})")


# ============================ КАРТА ЛОЯЛЬНОСТИ ==============================
def kb_card(has_card: bool) -> InlineKeyboardMarkup:
    if has_card:
        return InlineKeyboardMarkup(inline_keyboard=[[_btn("📱 Сменить номер карты", "lc:phone")]])
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("💳 Подключить карту", "lc:phone")]])


async def _send_card(message: Message, state: FSMContext, uid: int) -> None:
    card = guest_card(uid)
    if card:
        await message.answer(card_text(uid), reply_markup=kb_card(True))
        return
    await message.answer(
        "💎 <b>Карта лояльности Zig Zag</b>\n\n"
        "У вас пока нет карты. Оставьте номер телефона — он станет номером карты.\n"
        f"🎁 Кешбэк <b>{LOYALTY_PERCENT}%</b> баллами с каждого оплаченного заказа "
        f"(1 балл = 1 {CURRENCY}), оплатить баллами можно до {BONUS_MAX_PAY_PCT}% чека.",
        reply_markup=kb_card(False),
    )


@router.message(F.text == BTN_CARD)
@router.message(Command("card"))
async def on_my_card(message: Message, state: FSMContext):
    if message.chat.type != "private" or not message.from_user:
        return
    db_guest_touch(message.from_user)
    await _send_card(message, state, message.from_user.id)


@router.callback_query(F.data == "lc:phone")
async def cb_card_phone(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(next="card")
    await state.set_state(OrderFSM.phone)
    await cb.message.answer(
        "📱 Номер телефона станет номером карты. Надёжнее всего — кнопкой "
        "«📱 Отправить мой номер» (номер подтверждается Telegram).",
        reply_markup=kb_phone(),
    )


# --------------------------- /guest — карта гостя (админ) --------------------
class GuestFSM(StatesGroup):
    search = State()   # ввод телефона / @ника
    accrue = State()   # сумма чека вне бота → кешбэк
    spend = State()    # сколько баллов списать


def kb_guest_admin(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📊 Аналитика гостя", f"ga:an:{uid}")],
        [_btn("➕ Начислить за чек", f"ga:acc:{uid}"), _btn("➖ Списать баллы", f"ga:sp:{uid}")],
        [_btn("🔄 Обновить", f"ga:v:{uid}"), _btn("✖ Закрыть", "ga:close")],
    ])


async def _admin_guest_search(message: Message, query: str) -> None:
    found = db_guest_search(query)
    if not found:
        await message.answer(
            f"🔍 По запросу «{esc(query)}» карт не найдено.\n"
            "Ищите по телефону (можно последние цифры) или @нику: /guest +79001234567"
        )
        return
    if len(found) == 1:
        uid = int(found[0]["user_id"])
        await message.answer(card_text(uid, for_admin=True), reply_markup=kb_guest_admin(uid))
        return
    rows = [
        [_btn(f"{g.get('phone') or '—'} · {g.get('first_name') or ''}"
              + (f" @{g['username']}" if g.get("username") else ""), f"ga:v:{g['user_id']}")]
        for g in found[:10]
    ]
    await message.answer(
        f"🔍 Найдено карт: {len(found)}. Выберите:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(Command("guest"))
async def cmd_guest(message: Message, command: CommandObject, state: FSMContext):
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    await state.clear()
    q = (command.args or "").strip()
    if q:
        await _admin_guest_search(message, q)
        return
    await state.set_state(GuestFSM.search)
    await message.answer(
        "🔍 Введите телефон гостя (можно последние 4+ цифры) или @ник:",
        reply_markup=_cancel_kb("ga:close"),
    )


@router.message(GuestFSM.search, F.text)
async def guest_search_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    await state.clear()
    await _admin_guest_search(message, (message.text or "").strip())


@router.callback_query(F.data.startswith("ga:"))
async def cb_guest_admin(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("⛔ Только для администратора", show_alert=True)
        return
    parts = cb.data.split(":")
    try:
        if parts[1] == "close":
            await state.clear()
            await cb.answer("Закрыли")
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            return
        uid = int(parts[2])
        if not guest_card(uid):
            await cb.answer("Карта не найдена", show_alert=True)
            return
        if parts[1] == "v":
            await state.clear()
            await cb.answer()
            try:
                await cb.message.edit_text(card_text(uid, for_admin=True),
                                           reply_markup=kb_guest_admin(uid))
            except TelegramBadRequest:
                pass
            return
        if parts[1] == "o":  # из /loyalty — карта отдельным сообщением
            await state.clear()
            await cb.answer()
            await cb.message.answer(card_text(uid, for_admin=True),
                                    reply_markup=kb_guest_admin(uid))
            return
        if parts[1] == "an":
            await state.clear()
            await cb.answer()
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [_btn("🔙 К карте", f"ga:v:{uid}"), _btn("✖ Закрыть", "ga:close")],
            ])
            try:
                await cb.message.edit_text(guest_analytics_text(uid), reply_markup=kb)
            except TelegramBadRequest:
                pass
            return
        if parts[1] in ("acc", "sp"):
            await cb.answer()
            await state.set_state(GuestFSM.accrue if parts[1] == "acc" else GuestFSM.spend)
            await state.update_data(ga_uid=uid, ed_msg=cb.message.message_id)
            prompt = (
                f"➕ Сумма чека в {CURRENCY}, оплаченного мимо бота — начислим "
                f"{LOYALTY_PERCENT}% баллами:"
                if parts[1] == "acc"
                else f"➖ Сколько баллов списать? Баланс: {_fmt_pts(db_bonus_balance(uid))}"
            )
            await cb.message.edit_text(prompt, reply_markup=_cancel_kb(f"ga:v:{uid}"))
            return
        await cb.answer()
    except (ValueError, IndexError):
        await cb.answer("Некорректный запрос", show_alert=True)
    except Exception:
        logger.exception("Ошибка в карте гостя: %s", cb.data)
        try:
            await cb.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.message(Command("loyalty"))
async def cmd_loyalty(message: Message, state: FSMContext):
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    await state.clear()
    o = loyalty_overview(30)
    png = await _render_chart(loyalty_chart_png, o)
    await _answer_chart_and_text(message, png, "💎 Лояльность · 30 дн.",
                                 loyalty_text(o), kb_loyalty(o, "30"))


@router.callback_query(F.data.startswith("la:"))
async def cb_loyalty(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("⛔ Только для администратора", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    if key not in _LA_PERIODS:
        await cb.answer("Некорректный период", show_alert=True)
        return
    o = loyalty_overview(_LA_PERIODS[key])
    await cb.answer()
    try:
        await cb.message.edit_text(loyalty_text(o), reply_markup=kb_loyalty(o, key))
    except TelegramBadRequest:
        return  # тот же период — картинка уже актуальна
    png = await _render_chart(loyalty_chart_png, o)
    await _update_chart(cb.bot, cb.message.chat.id, cb.message.message_id, png,
                        "💎 Лояльность · " + ("всё время" if key == "all" else f"{key} дн."))


async def _notify_guest(bot, uid: int, text: str) -> None:
    try:
        await bot.send_message(uid, text)
    except Exception:
        logger.info("Не удалось уведомить гостя %s о баллах", uid)


@router.message(GuestFSM.accrue, F.text)
async def guest_accrue_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    data = await state.get_data()
    uid = data.get("ga_uid")
    v = _parse_num((message.text or "").strip())
    if not uid or not guest_card(uid):
        await state.clear()
        await message.answer("Карта не найдена — откройте /guest заново.")
        return
    if v is None or v < 1 or v > 1_000_000 or v != int(v):
        await message.answer("Введите сумму чека целым числом (например 3500), либо ✖ Отмена.")
        return
    pts = loyalty_offline_accrue(int(uid), int(v), message.from_user.id)
    await state.clear()
    await _edit_or_send(
        message, data,
        f"✅ Начислено +{_fmt_pts(pts)} баллов за чек {_fmt_money(int(v))}\n\n"
        + card_text(int(uid), for_admin=True),
        kb_guest_admin(int(uid)),
    )
    if pts:
        await _notify_guest(
            message.bot, int(uid),
            f"💎 Начислено +{_fmt_pts(pts)} баллов за чек {_fmt_money(int(v))}. "
            f"Баланс: {_fmt_pts(db_bonus_balance(int(uid)))}",
        )


@router.message(GuestFSM.spend, F.text)
async def guest_spend_input(message: Message, state: FSMContext):
    if not await _admin_msg_guard(message, state):
        return
    data = await state.get_data()
    uid = data.get("ga_uid")
    v = _parse_num((message.text or "").strip())
    if not uid or not guest_card(uid):
        await state.clear()
        await message.answer("Карта не найдена — откройте /guest заново.")
        return
    if v is None or v < 1 or v != int(v):
        await message.answer("Введите количество баллов целым числом, либо ✖ Отмена.")
        return
    if not loyalty_manual_spend(int(uid), int(v), message.from_user.id):
        await message.answer(
            f"Недостаточно баллов: баланс {_fmt_pts(db_bonus_balance(int(uid)))}. "
            "Введите меньше, либо ✖ Отмена."
        )
        return
    await state.clear()
    await _edit_or_send(
        message, data,
        f"✅ Списано {_fmt_pts(int(v))} баллов\n\n" + card_text(int(uid), for_admin=True),
        kb_guest_admin(int(uid)),
    )
    await _notify_guest(
        message.bot, int(uid),
        f"💎 Списано {_fmt_pts(int(v))} баллов. Баланс: {_fmt_pts(db_bonus_balance(int(uid)))}",
    )


# ========================= ЗАКАЗ ПО АКЦИИ («Выгода») ========================
# Отдельная корзина pcart и отдельный заказ kind='promo'.
def _promo_text() -> str:
    hh = hh_active()
    lines = [
        "🎁 <b>Заказ по акции</b>",
        "Отдельный заказ: акции не суммируются, баллы не начисляются и не списываются.",
        "",
        "☀️ <b>Счастливые часы</b> — по будням 15:00–18:00: "
        + ("<b>сейчас действуют</b> ✅" if hh else "сейчас не действуют"),
        "🔥 <b>Комбо на электронной чаше ХУКА Про</b> — в любое время",
    ]
    return "\n".join(lines)


def kb_promo(pcart_count: int = 0) -> InlineKeyboardMarkup:
    reserved = reserved_snapshot()
    rows: list[list[InlineKeyboardButton]] = []
    hh = hh_active()

    def _row(pid: str):
        p = promo_item(pid)
        if not p or not item_in_stock(pid, reserved):
            return None
        old = f" (вместо {_fmt_money(p['old'])})" if p["old"] > p["price"] else ""
        return [_btn(f"＋ {p['name']} — {_fmt_money(p['price'])}{old}", f"p:a:{pid}")]

    for pid, p in PROMO_ITEMS.items():
        if p["hh"] and not hh:
            continue
        r = _row(pid)
        if r:
            rows.append(r)
    if (hh or PROMO_TEA_ALWAYS) and _items_in_cats(PROMO_TEA_HOOKAH_CATS) and _items_in_cats(PROMO_TEA_CATS):
        rows.append([_btn("🍵 Любой кальян по полной цене + чай в подарок", "p:tea")])
    rows.append([_btn("📅 Аренда VIP-комнаты / PlayStation", "p:rent")])
    label = "🛒 Корзина акций" + (f" ({pcart_count})" if pcart_count else "")
    rows.append([_btn(label, "p:cart")])
    rows.append([_btn("🔙 В меню", "m")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_pcart(cart: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(len(cart)):
        rows.append([
            _btn("➖", f"p:cd:{idx}"),
            _btn(f"{cart[idx]['qty']} шт.", "noop"),
            _btn("➕", f"p:ci:{idx}"),
            _btn("🗑", f"p:cr:{idx}"),
        ])
    rows.append([_btn("✅ Оформить заказ по акции", "p:co")])
    rows.append([_btn("🗑 Очистить", "p:cx")])
    rows.append([_btn("🔙 К акциям", "p:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_pconfirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Отправить заказ", "p:so")],
        [_btn("🔙 Назад к корзине", "p:cart")],
    ])


def _promo_revalidate(cart: list[dict]) -> tuple[list[dict], list[str]]:
    """Перед отправкой: акция существует, цена актуальна, счастливые часы
    ещё идут (с запасом HH_GRACE_MIN). Возвращает (корзина, что убрали)."""
    ok: list[dict] = []
    bad: list[str] = []
    hh = hh_active(HH_GRACE_MIN)
    for it in cart:
        p = promo_item(str(it.get("id") or ""))
        if not p:
            bad.append(f"• {esc(it.get('name', '?'))} — акция недоступна")
            continue
        if p["hh"] and not hh:
            bad.append(f"• {esc(p['name'])} — счастливые часы закончились")
            continue
        ok.append({"id": p["id"], "name": p["name"], "price": p["price"],
                   "qty": max(1, int(it.get("qty") or 1))})
    return ok, bad


async def _pcart(state: FSMContext) -> list[dict]:
    return list((await state.get_data()).get("pcart") or [])


async def _render_pcart(cb: CallbackQuery, state: FSMContext) -> None:
    cart = await _pcart(state)
    table = await _table(state)
    if not cart:
        try:
            await cb.message.edit_text(_promo_text() + "\n\n🛒 Корзина акций пуста.",
                                       reply_markup=kb_promo(0))
        except TelegramBadRequest:
            pass
        return
    try:
        await cb.message.edit_text(
            cart_text(cart, table, title="🎁 <b>Корзина акций</b>"), reply_markup=kb_pcart(cart)
        )
    except TelegramBadRequest:
        pass


async def _promo_add(cb: CallbackQuery, state: FSMContext, pid: str) -> None:
    p = promo_item(pid)
    if not p:
        await cb.answer(ERR_MENU, show_alert=True)
        return
    if p["hh"] and not hh_active():
        await cb.answer("☀️ Счастливые часы сейчас не действуют (будни 15:00–18:00)",
                        show_alert=True)
        return
    if not await _table(state):
        await cb.answer(ASK_TABLE_FIRST, show_alert=True)
        return
    cart = await _pcart(state)
    if not _is_admin(cb.from_user.id) and _cart_count(cart) >= ORDER_MAX_ITEMS:
        await cb.answer(f"⚠️ В одном заказе не больше {ORDER_MAX_ITEMS} позиций", show_alert=True)
        return
    limit = max_available(pid)
    in_cart = next((i["qty"] for i in cart if i["id"] == pid), 0)
    if limit is not None and in_cart + 1 > limit:
        await cb.answer(f"⚠️ По остаткам на складе можно не более {limit} шт.", show_alert=True)
        return
    found = next((i for i in cart if i["id"] == pid), None)
    if found:
        found["qty"] += 1
    else:
        cart.append({"id": pid, "name": p["name"], "price": p["price"], "qty": 1})
    await state.update_data(pcart=cart)
    await cb.answer(f"«{p['name']}» — в корзине акций ({_cart_count(cart)} шт.)")


@router.callback_query(F.data.startswith("p:"))
async def cb_promo(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    try:
        if act in ("home", "new"):
            await cb.answer()
            cart = await _pcart(state)
            text, kb = _promo_text(), kb_promo(_cart_count(cart))
            if act == "new":  # из-под фото «Выгода» — новым сообщением
                await cb.message.answer(text, reply_markup=kb)
            else:
                try:
                    await cb.message.edit_text(text, reply_markup=kb)
                except TelegramBadRequest:
                    pass
            return
        if act == "rent":
            await _begin_flow("rsv", cb.message, state, cb=cb, strip=False)
            return
        if act == "a" and len(parts) == 3:
            await _promo_add(cb, state, parts[2])
            if _is_promo_id(parts[2]) and not parts[2].startswith("pr_t:"):
                try:
                    await cb.message.edit_reply_markup(
                        reply_markup=kb_promo(_cart_count(await _pcart(state)))
                    )
                except TelegramBadRequest:
                    pass
            return
        if act == "tea":
            if not (hh_active() or PROMO_TEA_ALWAYS):
                await cb.answer("☀️ Акция действует в счастливые часы (будни 15:00–18:00)",
                                show_alert=True)
                return
            await cb.answer()
            reserved = reserved_snapshot()
            rows = [
                [_btn(f"{h['name']} — {_fmt_money(h['price'])}", f"p:th:{h['id']}")]
                for h in _items_in_cats(PROMO_TEA_HOOKAH_CATS)
                if item_in_stock(h["id"], reserved)
            ]
            rows.append([_btn("🔙 К акциям", "p:home")])
            await cb.message.edit_text(
                "🍵 <b>Кальян по полной цене + чай в подарок</b>\n\nШаг 1 — выберите кальян:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            return
        if act == "th" and len(parts) == 3:
            hid = parts[2]
            hookah = next((i for i in _items_in_cats(PROMO_TEA_HOOKAH_CATS) if i["id"] == hid), None)
            if not hookah:
                await cb.answer(ERR_MENU, show_alert=True)
                return
            await cb.answer()
            reserved = reserved_snapshot()
            rows = [
                [_btn(f"🎁 {t['name']}", f"p:tt:{hid}:{t['id']}")]
                for t in _items_in_cats(PROMO_TEA_CATS)
                if item_in_stock(t["id"], reserved)
            ]
            rows.append([_btn("🔙 Другой кальян", "p:tea")])
            await cb.message.edit_text(
                f"🍵 {esc(hookah['name'])} — {_fmt_money(hookah['price'])}\n\n"
                "Шаг 2 — выберите чай в подарок:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            return
        if act == "tt" and len(parts) == 4:
            await _promo_add(cb, state, f"pr_t:{parts[2]}:{parts[3]}")
            try:
                await cb.message.edit_text(_promo_text(),
                                           reply_markup=kb_promo(_cart_count(await _pcart(state))))
            except TelegramBadRequest:
                pass
            return
        if act == "cart":
            await cb.answer()
            await _render_pcart(cb, state)
            return
        if act in ("ci", "cd", "cr") and len(parts) == 3:
            cart = await _pcart(state)
            try:
                idx = int(parts[2])
            except ValueError:
                idx = -1
            if not 0 <= idx < len(cart):
                await cb.answer(ERR_MENU, show_alert=True)
                return
            if act == "ci":
                p = promo_item(str(cart[idx].get("id")))
                if not p:
                    await cb.answer(ERR_MENU, show_alert=True)
                    return
                if p["hh"] and not hh_active():
                    await cb.answer("☀️ Счастливые часы закончились", show_alert=True)
                    return
                if not _is_admin(cb.from_user.id) and _cart_count(cart) >= ORDER_MAX_ITEMS:
                    await cb.answer(f"⚠️ В одном заказе не больше {ORDER_MAX_ITEMS} позиций",
                                    show_alert=True)
                    return
                limit = max_available(p["id"])
                if limit is not None and cart[idx]["qty"] + 1 > limit:
                    await cb.answer(f"⚠️ По остаткам можно не более {limit} шт.", show_alert=True)
                    return
                cart[idx]["qty"] += 1
            elif act == "cd":
                cart[idx]["qty"] -= 1
                if cart[idx]["qty"] <= 0:
                    cart.pop(idx)
            else:
                cart.pop(idx)
            await state.update_data(pcart=cart)
            await cb.answer()
            await _render_pcart(cb, state)
            return
        if act == "cx":
            await state.update_data(pcart=[])
            await cb.answer("Корзина акций очищена")
            await _render_pcart(cb, state)
            return
        if act == "co":
            cart = await _pcart(state)
            if not cart:
                await cb.answer(CART_EMPTY, show_alert=True)
                return
            table = await _table(state)
            if not table:
                await cb.answer()
                await state.set_state(OrderFSM.table)
                await cb.message.edit_text(ASK_TABLE)
                return
            await cb.answer()
            await cb.message.edit_text(confirm_text(cart, table, promo=True),
                                       reply_markup=kb_pconfirm())
            return
        if act == "so":
            await _submit_order(cb, state, "promo")
            return
        await cb.answer()
    except Exception:
        logger.exception("Ошибка в заказе по акции: %s", cb.data)
        try:
            await cb.answer("⚠️ Ошибка — попробуйте ещё раз", show_alert=True)
        except Exception:
            pass


# =============================== ПРЕДЗАКАЗ ==================================
# Гость с картой собирает заказ заранее — к времени прихода (сегодня/завтра)
# или к своей активной брони. Отдельная корзина precart, заказ kind='pre'
# со временем прихода arrive_at. Оплата — на месте, кешбэк как у обычного.
# Отменить сам гость может, пока заказ в статусе «Принят» (до «Готов»).
PRE_LEAD_MIN = 30          # время прихода — не раньше чем через 30 мин
PRE_SUBMIT_MIN = 15        # при отправке до прихода должно оставаться ≥15 мин
PRE_LAST_BEFORE_CLOSE = 60 # последний слот — за час до закрытия (02:00)
PRE_MAX_ACTIVE = 2         # одновременно активных предзаказов у гостя
PRE_TABLE = "предзаказ"    # orders.table_no для предзаказа


def _place_short(table_no: str) -> str:
    return PRE_TABLE if table_no == PRE_TABLE else f"стол {table_no}"


def _pre_when(o: dict) -> str:
    """«пн 12.10 · 20:00» по arrive_at заказа."""
    try:
        a = datetime.strptime(str(o.get("arrive_at"))[:16], "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "—"
    return f"{_wd(a)} {a:%d.%m} · {a:%H:%M}"


def _pre_days() -> list:
    """Рабочие дни для предзаказа: текущий (включая «эту ночь») и следующий."""
    b = _biz_today()
    return [b, b + timedelta(days=1)]


def _pre_slots(d) -> list[str]:
    """Свободные для предзаказа слоты дня d: в часы работы, ≥ сейчас + PRE_LEAD_MIN."""
    edge = _now_dt() + timedelta(minutes=PRE_LEAD_MIN)
    close = datetime(d.year, d.month, d.day) + timedelta(days=1, hours=RSV_CLOSE_H)
    last = close - timedelta(minutes=PRE_LAST_BEFORE_CLOSE)
    out = []
    for t in _slots():
        a = _arrival(f"{d:%Y-%m-%d}", t)
        if edge <= a <= last:
            out.append(t)
    return out


def db_pre_active(uid: int) -> list[dict]:
    """Активные предзаказы гостя (не оплачены, не отменены, приход не в прошлом)."""
    since = (_now_dt() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, total, status, arrive_at, rsv_id, message_id FROM orders"
            " WHERE user_id = ? AND kind = 'pre' AND status IN ('accepted', 'ready', 'issued')"
            " AND arrive_at >= ? ORDER BY arrive_at",
            (uid, since),
        )]


def db_pre_cancel(order_id: int, uid: int) -> bool:
    """Отмена предзаказа гостем — только свой и только пока «Принят». Атомарно:
    если бармен уже нажал «Готов», ничего не меняем."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = ?, issued_at = NULL"
            " WHERE id = ? AND user_id = ? AND kind = 'pre' AND status = 'accepted'",
            (_now(), int(order_id), int(uid)),
        )
        return cur.rowcount == 1


def _pre_rsv_options(uid: int) -> list[dict]:
    """Активные брони гостя, к которым ещё можно сделать предзаказ."""
    days = {f"{d:%Y-%m-%d}" for d in _pre_days()}
    edge = _now_dt() + timedelta(minutes=PRE_LEAD_MIN)
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, kind, date, time, status FROM reservations"
            " WHERE user_id = ? AND status IN ('new', 'confirmed') ORDER BY date, time",
            (uid,),
        )]
    out = []
    for r in rows:
        try:
            if r["date"] in days and _arrival(r["date"], r["time"]) >= edge:
                out.append(r)
        except (ValueError, TypeError):
            continue
    return out


def _pre_draft_when(pre: dict) -> str:
    return _rsv_when_text(pre["date"], pre["time"])


def pre_home_text(uid: int, data: dict) -> tuple[str, InlineKeyboardMarkup]:
    active = db_pre_active(uid)
    L = ["📝 <b>Предзаказ</b>", "",
         "Соберите заказ заранее — к вашему приходу всё будет готово. "
         "Оплата — на месте" + (f", кешбэк {LOYALTY_PERCENT}% как обычно." if LOYALTY_PERCENT
                                else ".")]
    rows: list[list[InlineKeyboardButton]] = []
    if active:
        L += ["", "<b>Ваши предзаказы:</b>"]
        for o in active:
            link = f" · к брони #{o['rsv_id']}" if o.get("rsv_id") else ""
            L.append(f"• #{o['id']} · {_pre_when(o)}{link} · {_fmt_money(o['total'])} · "
                     f"{STATUSES.get(o['status'], o['status'])}")
            if o["status"] == "accepted":
                rows.append([_btn(f"❌ Отменить #{o['id']}", f"po:x:{o['id']}")])
    pre = data.get("pre") or {}
    pcart = data.get("precart") or []
    if pre.get("date") and pcart:
        L += ["", f"🛒 Черновик к {_pre_draft_when(pre)}: {_cart_count(pcart)} шт."]
        rows.append([_btn("🛒 Продолжить черновик", "po:cart")])
    if len(active) >= PRE_MAX_ACTIVE and not _is_admin(uid):
        L += ["", f"ℹ️ Одновременно можно держать не больше {PRE_MAX_ACTIVE} предзаказов."]
    else:
        for r in _pre_rsv_options(uid):
            rows.append([_btn(
                f"📅 К брони #{r['id']} · {_rsv_when_text(r['date'], r['time'])}",
                f"po:r:{r['id']}")])
        if any(_pre_slots(d) for d in _pre_days()):
            rows.append([_btn("⏰ Выбрать время прихода", "po:time")])
        else:
            L += ["", "⏰ На сегодня время для предзаказа уже вышло — загляните завтра."]
    rows.append([_btn("✖ Закрыть", "po:close")])
    return "\n".join(L), InlineKeyboardMarkup(inline_keyboard=rows)


def kb_pre_days() -> InlineKeyboardMarkup:
    rows = [[_btn(_date_label(d), f"po:d:{d:%Y-%m-%d}")] for d in _pre_days() if _pre_slots(d)]
    rows.append([_btn("🔙 Назад", "po:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_pre_slots(d) -> InlineKeyboardMarkup:
    btns = [_btn(t, f"po:t:{d:%Y%m%d}{t.replace(':', '')}") for t in _pre_slots(d)]
    rows = [btns[i : i + 4] for i in range(0, len(btns), 4)]
    rows.append([_btn("🔙 Другой день", "po:time")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_pre_cats(n: int) -> InlineKeyboardMarkup:
    rows = [[_btn(f"{c['emoji']} {c['name']}", f"po:c:{c['id']}")] for c in MENU["categories"]]
    rows.append([_btn("🛒 Корзина предзаказа" + (f" ({n})" if n else ""), "po:cart")])
    rows.append([_btn("⏰ Изменить время", "po:time"), _btn("🔙 Назад", "po:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_pre_cat(cat_id: str, n: int) -> InlineKeyboardMarkup:
    cat = f_category(cat_id) or {"items": []}
    reserved = reserved_snapshot()
    rows = [[_btn(f"＋ {it['name']} — {_fmt_money(it['price'])}", f"po:a:{it['id']}")]
            for it in cat["items"] if item_in_stock(it["id"], reserved)]
    rows.append([_btn("🛒 Корзина предзаказа" + (f" ({n})" if n else ""), "po:cart")])
    rows.append([_btn("🔙 Разделы", "po:cats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_pre_cart(cart: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(len(cart)):
        rows.append([
            _btn("➖", f"po:cd:{idx}"),
            _btn(f"{cart[idx]['qty']} шт.", "noop"),
            _btn("➕", f"po:ci:{idx}"),
            _btn("🗑", f"po:cr:{idx}"),
        ])
    rows.append([_btn("✅ Оформить предзаказ", "po:co")])
    rows.append([_btn("➕ Добавить ещё", "po:cats"), _btn("🗑 Очистить", "po:cx")])
    rows.append([_btn("🔙 Назад", "po:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pre_head(pre: dict) -> str:
    s = f"📝 <b>Предзаказ</b> к <b>{_pre_draft_when(pre)}</b>"
    if pre.get("rsv_id"):
        s += f"\n📅 К брони #{pre['rsv_id']}"
    return s


def pre_confirm_text(cart: list[dict], pre: dict) -> str:
    L = ["✅ <b>Подтверждение предзаказа</b>", "",
         f"⏰ К приходу: <b>{_pre_draft_when(pre)}</b>"]
    if pre.get("rsv_id"):
        L.append(f"📅 К брони #{pre['rsv_id']}")
    L.append("")
    for i in cart:
        L.append(f"• {esc(i['name'])} × {i['qty']} — {_fmt_money(i['price'] * i['qty'])}")
    L += ["", f"💰 <b>К оплате на месте: {_fmt_money(_cart_total(cart))}</b>", "",
          "Отменить можно здесь же, пока бармен не отметил заказ «Готов».", "",
          "Отправить предзаказ?"]
    return "\n".join(L)


async def _pre_edit(cb: CallbackQuery, text: str, kb) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            await cb.message.answer(text, reply_markup=kb)


def _pre_valid(pre: dict | None) -> bool:
    """Черновик ещё актуален: день в горизонте, время в будущем."""
    if not pre or not pre.get("date") or not pre.get("time"):
        return False
    try:
        a = _arrival(pre["date"], pre["time"])
    except (ValueError, TypeError):
        return False
    return (pre["date"] in {f"{d:%Y-%m-%d}" for d in _pre_days()}
            and a >= _now_dt() + timedelta(minutes=PRE_SUBMIT_MIN))


def _pre_need_card_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("📱 Подключить карту", "po:card")]])


PRE_NEED_CARD = ("📝 <b>Предзаказ</b> доступен гостям с картой лояльности — так мы "
                 "узнаем вас при приходе. Подключение — один шаг: номер телефона.")


async def _pre_home(message: Message, state: FSMContext, uid: int,
                    cb: CallbackQuery | None = None) -> None:
    if not guest_card(uid):
        if cb:
            await _pre_edit(cb, PRE_NEED_CARD, _pre_need_card_kb())
        else:
            await message.answer(PRE_NEED_CARD, reply_markup=_pre_need_card_kb())
        return
    text, kb = pre_home_text(uid, await state.get_data())
    if cb:
        await _pre_edit(cb, text, kb)
    else:
        await message.answer(text, reply_markup=kb)


async def _pre_render_cart(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    cart = list(data.get("precart") or [])
    pre = data.get("pre") or {}
    if not cart:
        await _pre_edit(cb, _pre_head(pre) + "\n\n🛒 Корзина предзаказа пуста. Выберите раздел:",
                        kb_pre_cats(0))
        return
    await _pre_edit(cb, cart_text(cart, None, title=_pre_head(pre)), kb_pre_cart(cart))


async def _submit_preorder(cb: CallbackQuery, state: FSMContext) -> None:
    uid = cb.from_user.id
    lock = _user_lock(uid)
    if lock.locked():
        await cb.answer("⏳ Уже отправляем…")
        return
    async with lock:
        card = guest_card(uid)
        if not card:
            await cb.answer("Нужна карта лояльности", show_alert=True)
            await _pre_edit(cb, PRE_NEED_CARD, _pre_need_card_kb())
            return
        data = await state.get_data()
        cart = list(data.get("precart") or [])
        pre = dict(data.get("pre") or {})
        if not cart:
            await cb.answer(CART_EMPTY, show_alert=True)
            return
        if not _pre_valid(pre):
            await cb.answer("⏰ Это время уже не подходит — выберите другое", show_alert=True)
            await state.update_data(pre={})
            await _pre_edit(cb, "⏰ <b>Когда придёте?</b>", kb_pre_days())
            return
        rsv_id = pre.get("rsv_id")
        if rsv_id and not any(r["id"] == rsv_id for r in _pre_rsv_options(uid)):
            await cb.answer("📅 Эта бронь уже неактивна — выберите время прихода",
                            show_alert=True)
            await state.update_data(pre={})
            await _pre_edit(cb, "⏰ <b>Когда придёте?</b>", kb_pre_days())
            return
        if not _is_admin(uid) and len(db_pre_active(uid)) >= PRE_MAX_ACTIVE:
            await cb.answer(f"Одновременно можно держать не больше {PRE_MAX_ACTIVE} "
                            "предзаказов", show_alert=True)
            return
        err = _order_limits_error(uid, cart)
        if err:
            await cb.answer(err, show_alert=True)
            return
        problems = cart_stock_problems(cart)
        if problems:
            await cb.answer("⚠️ Не хватает на складе", show_alert=True)
            await cb.message.answer("⚠️ <b>Не хватает на складе:</b>\n" + "\n".join(problems)
                                    + "\n\nУменьшите количество или выберите другое.")
            await _pre_render_cart(cb, state)
            return
        await cb.answer("Отправляем…")
        total = _cart_total(cart)
        arrive = _arrival(pre["date"], pre["time"]).strftime("%Y-%m-%d %H:%M:%S")
        user = cb.from_user
        order_id = db_add_order(table_no=PRE_TABLE, total=total, user_id=uid,
                                username=user.username if user else None,
                                phone=card["phone"], kind="pre",
                                arrive_at=arrive, rsv_id=rsv_id)
        db_add_items(order_id, cart)
        order = db_get_order(order_id)
        try:
            sent = await cb.bot.send_message(CHANNEL_ID, order_text(order),
                                             reply_markup=kb_status(order_id))
        except Exception:
            logger.exception("Не удалось отправить предзаказ #%s в канал", order_id)
            db_delete_order(order_id)
            await _pre_edit(cb, "⚠️ Не удалось отправить предзаказ. Попробуйте ещё раз "
                                "или сообщите администратору.",
                            InlineKeyboardMarkup(inline_keyboard=[
                                [_btn("✅ Отправить ещё раз", "po:so")],
                                [_btn("🔙 К корзине", "po:cart")]]))
            return
        db_set_message_id(order_id, sent.message_id)
        await state.update_data(precart=[], pre={})
        db_guest_touch(user)
        text = (f"✅ Предзаказ <b>#{order_id}</b> отправлен!\n"
                f"⏰ К приходу: <b>{_pre_when(order)}</b>\n"
                + (f"📅 К брони #{rsv_id}\n" if rsv_id else "")
                + f"💰 Сумма: <b>{_fmt_money(total)}</b> — оплата на месте\n\n"
                "Бармен подготовит всё к вашему приходу. Статус пришлём сюда.")
        if LOYALTY_PERCENT:
            text += f"\n💎 После оплаты начислим кешбэк {LOYALTY_PERCENT}% баллами."
        await _pre_edit(cb, text, InlineKeyboardMarkup(inline_keyboard=[
            [_btn("❌ Отменить предзаказ", f"po:x:{order_id}")],
            [_btn("📝 Мои предзаказы", "po:home")]]))


async def _pre_cancel(cb: CallbackQuery, order_id: int) -> None:
    uid = cb.from_user.id
    o = db_get_order(order_id)
    if not o or o.get("user_id") != uid or o.get("kind") != "pre":
        await cb.answer("Предзаказ не найден", show_alert=True)
        return
    if not db_pre_cancel(order_id, uid):
        st = STATUSES.get(o["status"], o["status"])
        await cb.answer(f"Отменить уже нельзя (статус: {st}). Если планы изменились — "
                        "напишите администратору.", show_alert=True)
        return
    # как отмена персоналом: склад (если списан) и баллы — ровно один раз
    if db_claim_stock_flag(order_id, 0):
        await _stock_alerts(cb.bot, stock_apply_order(o, +1))
    loyalty_on_refund(order_id)
    db_set_bonus_pending(order_id, 0)
    o = db_get_order(order_id)
    await cb.answer("Предзаказ отменён")
    if o.get("message_id"):
        try:
            await cb.bot.edit_message_text(order_text(o), chat_id=CHANNEL_ID,
                                           message_id=o["message_id"],
                                           reply_markup=kb_status(order_id))
        except Exception:
            logger.info("Не удалось обновить сообщение предзаказа #%s", order_id)
    try:
        await cb.bot.send_message(
            CHANNEL_ID, f"❌ Гость отменил предзаказ <b>#{order_id}</b> ({_pre_when(o)})",
            reply_to_message_id=o.get("message_id") or None)
    except Exception:
        logger.info("Не удалось уведомить канал об отмене предзаказа #%s", order_id)
    await _pre_edit(cb, f"❌ Предзаказ <b>#{order_id}</b> отменён.",
                    InlineKeyboardMarkup(inline_keyboard=[[_btn("📝 Предзаказ", "po:home")]]))


@router.message(F.text == BTN_PRE)
@router.message(Command("preorder"))
async def on_btn_pre(message: Message, state: FSMContext):
    if message.chat.type != "private" or not message.from_user:
        return
    await state.set_state(None)
    await _begin_flow("pre", message, state)


@router.callback_query(F.data.startswith("po:"))
async def cb_preorder(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    uid = cb.from_user.id
    try:
        if act == "card":
            await cb.answer()
            await state.update_data(next="pre")
            await state.set_state(OrderFSM.phone)
            await cb.message.answer(
                "📱 Номер телефона станет номером карты. Надёжнее всего — кнопкой "
                "«📱 Отправить мой номер» (номер подтверждается Telegram).",
                reply_markup=kb_phone(),
            )
            return
        if act == "close":
            await cb.answer()
            try:
                await cb.message.delete()
            except TelegramBadRequest:
                try:
                    await cb.message.edit_reply_markup(reply_markup=None)
                except TelegramBadRequest:
                    pass
            return
        if act == "x" and len(parts) == 3:
            await _pre_cancel(cb, int(parts[2]))
            return
        # дальше — только с картой
        if not guest_card(uid):
            await cb.answer()
            await _pre_edit(cb, PRE_NEED_CARD, _pre_need_card_kb())
            return
        if act == "home":
            await cb.answer()
            await _pre_home(cb.message, state, uid, cb=cb)
            return
        if act == "time":
            await cb.answer()
            if not any(_pre_slots(d) for d in _pre_days()):
                await _pre_edit(cb, "⏰ Время для предзаказа на сегодня и завтра вышло.",
                                InlineKeyboardMarkup(inline_keyboard=[
                                    [_btn("🔙 Назад", "po:home")]]))
                return
            await _pre_edit(cb, "⏰ <b>Когда придёте?</b>\n\nВыберите день:", kb_pre_days())
            return
        if act == "d" and len(parts) == 3:
            try:
                d = datetime.strptime(parts[2], "%Y-%m-%d").date()
            except ValueError:
                await cb.answer("Некорректная дата", show_alert=True)
                return
            if d not in _pre_days() or not _pre_slots(d):
                await cb.answer("На этот день предзаказ уже недоступен", show_alert=True)
                return
            await cb.answer()
            await _pre_edit(cb, f"⏰ <b>{_date_label(d)}</b>\n\nВо сколько придёте?",
                            kb_pre_slots(d))
            return
        if act == "t" and len(parts) == 3 and re.fullmatch(r"\d{12}", parts[2]):
            raw = parts[2]
            date_s = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
            time_s = f"{raw[8:10]}:{raw[10:12]}"
            try:
                d = datetime.strptime(date_s, "%Y-%m-%d").date()
            except ValueError:
                await cb.answer("Некорректная дата", show_alert=True)
                return
            if d not in _pre_days() or time_s not in _pre_slots(d):
                await cb.answer("Это время уже недоступно — выберите другое", show_alert=True)
                return
            pre = {"date": date_s, "time": time_s}
            await state.update_data(pre=pre)
            await cb.answer(f"Ждём вас к {time_s}")
            n = _cart_count((await state.get_data()).get("precart") or [])
            await _pre_edit(cb, _pre_head(pre) + "\n\nВыберите раздел:", kb_pre_cats(n))
            return
        if act == "r" and len(parts) == 3 and parts[2].isdigit():
            r = next((x for x in _pre_rsv_options(uid) if x["id"] == int(parts[2])), None)
            if not r:
                await cb.answer("Эта бронь недоступна для предзаказа", show_alert=True)
                return
            pre = {"date": r["date"], "time": r["time"], "rsv_id": r["id"]}
            await state.update_data(pre=pre)
            await cb.answer()
            n = _cart_count((await state.get_data()).get("precart") or [])
            await _pre_edit(cb, _pre_head(pre) + "\n\nВыберите раздел:", kb_pre_cats(n))
            return
        # каталог/корзина — нужен выбранный момент прихода
        data = await state.get_data()
        pre = data.get("pre") or {}
        if not _pre_valid(pre):
            await cb.answer("Сначала выберите время прихода", show_alert=True)
            await state.update_data(pre={})
            await _pre_home(cb.message, state, uid, cb=cb)
            return
        cart = list(data.get("precart") or [])
        if act == "cats":
            await cb.answer()
            await _pre_edit(cb, _pre_head(pre) + "\n\nВыберите раздел:",
                            kb_pre_cats(_cart_count(cart)))
            return
        if act == "c" and len(parts) == 3:
            cat = f_category(parts[2])
            if not cat:
                await cb.answer(ERR_MENU, show_alert=True)
                return
            await cb.answer()
            await _pre_edit(cb, f"{_pre_head(pre)}\n\n{cat['emoji']} <b>{esc(cat['name'])}</b>",
                            kb_pre_cat(cat["id"], _cart_count(cart)))
            return
        if act == "a" and len(parts) == 3:
            item = f_find_item(parts[2])
            if not item:
                await cb.answer(ERR_MENU, show_alert=True)
                return
            if not _is_admin(uid) and _cart_count(cart) >= ORDER_MAX_ITEMS:
                await cb.answer(f"⚠️ В одном заказе не больше {ORDER_MAX_ITEMS} позиций",
                                show_alert=True)
                return
            limit = max_available(item["id"])
            in_cart = next((i["qty"] for i in cart if i["id"] == item["id"]), 0)
            if limit is not None and in_cart + 1 > limit:
                await cb.answer(f"⚠️ По остаткам на складе можно не более {limit} шт.",
                                show_alert=True)
                return
            found = next((i for i in cart if i["id"] == item["id"]), None)
            if found:
                found["qty"] += 1
            else:
                cart.append({"id": item["id"], "name": item["name"], "price": item["price"],
                             "qty": 1})
            await state.update_data(precart=cart)
            await cb.answer(f"«{item['name']}» — в предзаказе ({_cart_count(cart)} шт.)")
            cat_id = _category_of_item(item["id"])
            if cat_id:
                try:
                    await cb.message.edit_reply_markup(
                        reply_markup=kb_pre_cat(cat_id, _cart_count(cart)))
                except TelegramBadRequest:
                    pass
            return
        if act == "cart":
            await cb.answer()
            await _pre_render_cart(cb, state)
            return
        if act in ("ci", "cd", "cr") and len(parts) == 3 and parts[2].isdigit():
            idx = int(parts[2])
            if not 0 <= idx < len(cart):
                await cb.answer("Корзина изменилась", show_alert=True)
                await _pre_render_cart(cb, state)
                return
            it = cart[idx]
            if act == "ci":
                if not _is_admin(uid) and _cart_count(cart) >= ORDER_MAX_ITEMS:
                    await cb.answer(f"⚠️ Не больше {ORDER_MAX_ITEMS} позиций", show_alert=True)
                    return
                limit = max_available(it["id"])
                if limit is not None and it["qty"] + 1 > limit:
                    await cb.answer(f"⚠️ По остаткам можно не более {limit} шт.",
                                    show_alert=True)
                    return
                it["qty"] += 1
            elif act == "cd":
                it["qty"] -= 1
                if it["qty"] <= 0:
                    cart.pop(idx)
            else:
                cart.pop(idx)
            await state.update_data(precart=cart)
            await cb.answer()
            await _pre_render_cart(cb, state)
            return
        if act == "cx":
            await state.update_data(precart=[])
            await cb.answer("Корзина предзаказа очищена")
            await _pre_render_cart(cb, state)
            return
        if act == "co":
            if not cart:
                await cb.answer(CART_EMPTY, show_alert=True)
                return
            await cb.answer()
            await _pre_edit(cb, pre_confirm_text(cart, pre), InlineKeyboardMarkup(
                inline_keyboard=[[_btn("✅ Отправить предзаказ", "po:so")],
                                 [_btn("🔙 К корзине", "po:cart")]]))
            return
        if act == "so":
            await _submit_preorder(cb, state)
            return
        await cb.answer()
    except Exception:
        logger.exception("Ошибка в предзаказе: %s", cb.data)
        try:
            await cb.answer("⚠️ Ошибка — попробуйте ещё раз", show_alert=True)
        except Exception:
            pass


# ------------------------------------- ID канала и служебные -----------------
@router.message(F.forward_origin)
async def on_forward(message: Message):
    origin = message.forward_origin
    chat = getattr(origin, "chat", None)
    if chat is not None:
        await message.answer(
            f"🪪 ID чата: <code>{chat.id}</code>\n\n"
            "Укажите его в Bothost как CHANNEL_ID и перезапустите бота."
        )
    else:
        await message.answer("Это не сообщение из канала. Перешлите именно пост из канала.")


async def _register_commands(bot: Bot) -> None:
    """Чистое меню «/»: гостям 4 команды, админам — 11. Вызывается при старте
    и при /help админа — список само-восстанавливается, если клиент Telegram
    показывает устаревший вариант или прошлая версия бота поставила мусор."""
    guest_cmds = [
        BotCommand(command="start", description="Меню / в начало"),
        BotCommand(command="help", description="Как пользоваться"),
        BotCommand(command="table", description="Сменить номер стола"),
        BotCommand(command="card", description="Моя карта лояльности"),
        BotCommand(command="preorder", description="Предзаказ к приходу"),
    ]
    admin_cmds = guest_cmds + [
        BotCommand(command="stats", description="Итоги дня"),
        BotCommand(command="orders", description="Последние заказы"),
        BotCommand(command="bookings", description="Ближайшие брони"),
        BotCommand(command="timing", description="Сроки выдачи заказов"),
        BotCommand(command="stock", description="Склад: остатки и себестоимость"),
        BotCommand(command="recipe", description="Рецепты позиций меню"),
        BotCommand(command="guest", description="Карта гостя: баллы по телефону/@нику"),
        BotCommand(command="loyalty", description="Аналитика программы лояльности"),
    ]
    # Полная зачистка старых списков (от прежних версий, в любой области)
    for _scope in (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
    ):
        try:
            await bot.delete_my_commands(scope=_scope)
        except Exception:
            pass
    for _aid in ADMIN_IDS:
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=_aid))
        except Exception:
            pass
    await bot.set_my_commands(guest_cmds)
    for _aid in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                admin_cmds, scope=BotCommandScopeChat(chat_id=_aid)
            )
        except Exception:
            logger.exception("Не удалось поставить админ-команды для %s", _aid)
    logger.info(
        "Меню команд: гостям %d, админам %d", len(guest_cmds), len(admin_cmds)
    )


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    table = await _table(state)
    await message.answer(
        "Как это работает:\n"
        "1. /start → выберите «Забронировать» или «Сделать заказ»\n"
        "2. Бот спросит про карту лояльности — оставьте телефон\n"
        "3. Заказ: укажите стол → выбирайте позиции → корзина → «Оформить»\n"
        "   Бронь: дата → время → гости → имя → длительность → подтверждение\n"
        "4. Заказ или бронь уйдут администратору, Вы получите смс с ответным статусом\n"
        f"5. 💎 Карта лояльности: кешбэк {LOYALTY_PERCENT}% баллами с оплаченных заказов, "
        f"оплатить баллами можно до {BONUS_MAX_PAY_PCT}% чека\n"
        "6. 🎁 Заказ по акции — в меню (отдельный заказ, баллы на него не действуют)\n"
        "7. 📝 Предзаказ (с картой) — соберите заказ заранее к времени прихода или к брони\n\n"
        "Команды: /start — в начало, /table — сменить стол, /card — моя карта, "
        "/preorder — предзаказ, "
        "📅 Бронирование — кнопка внизу",
        reply_markup=kb_main(table),
    )
    # Админ-команды показываем только админу
    if message.from_user.id in ADMIN_IDS:
        # Заодно заново ставим меню «/» — чтобы список само-восстановился,
        # если Telegram показывает устаревший вариант
        reg_ok = True
        try:
            await _register_commands(message.bot)
        except Exception:
            reg_ok = False
            logger.exception("Обновление меню команд (из /help) не удалось")
        reg_note = (
            "\n\n✅ Меню команд обновлено — наберите «/» в этом чате, "
            "список выпадет заново"
            if reg_ok
            else "\n\n⚠️ Не удалось обновить меню команд — проверьте логи Bothost"
        )
        await message.answer(
            "👤 <b>Для админа:</b>\n"
            "/stats — итоги дня\n"
            "/orders — последние заказы\n"
            "/bookings — ближайшие брони\n"
            "/timing — время выдачи заказов\n"
            "/stock — остатки и себестоимость\n"
            "/recipe — состав позиций для списания\n"
            "/guest — карта гостя по телефону/@нику: аналитика, баллы, начислить, списать\n"
            "/loyalty — аналитика лояльности: топ гостей, «спящие», баллы\n\n"
            f"🤖 Версия бота: <b>{BOT_VERSION}</b>" + reg_note
        )


@router.message(F.text, ~F.text.startswith("/"))
async def fallback(message: Message, state: FSMContext):
    data = await state.get_data()
    table = data.get("table")
    uid = message.from_user.id if message.from_user else None
    if table and (data.get("loyal_done") or guest_card(uid)):
        await message.answer(
            "Выберите раздел каталога 👇 или /help",
            reply_markup=kb_main(table),
        )
    else:
        await message.answer(WELCOME, reply_markup=kb_welcome())


# ================================= ЗАПУСК ===================================


async def main() -> None:
    # --- Предполётная проверка настроек -----------------------------------
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ Не задан BOT_TOKEN — добавьте переменную окружения BOT_TOKEN "
            "(в Bothost: раздел «Переменные») и перезапустите бота"
        )
    if not re.match(r"^\d{5,}:[A-Za-z0-9_-]{20,}$", BOT_TOKEN):
        raise SystemExit(
            "❌ BOT_TOKEN имеет неверный формат (ожидается вида 123456789:AA...). "
            "Проверьте, что скопирован целиком, без пробелов и кавычек"
        )

    logger.info(
        "Настройки: канал=%s, админов=%s, валюта=%s, db=%s",
        CHANNEL_ID or "❌ НЕ ЗАДАН",
        len(ADMIN_IDS),
        CURRENCY,
        DB_PATH,
    )
    logger.info("✅ Версия бота: %s", BOT_VERSION)
    logger.info(
        "Лояльность: кешбэк %d%%, баллами до %d%% чека; лимиты: %d поз./заказ, "
        "1 заказ в %d сек, броней на гостя %d",
        LOYALTY_PERCENT, BONUS_MAX_PAY_PCT, ORDER_MAX_ITEMS, ORDER_COOLDOWN_SEC,
        RSV_MAX_ACTIVE_PER_USER,
    )
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не задан — админ-команды (/stats, /stock, /guest…) недоступны")
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID не задан — заказы в канал отправляться не будут!")

    db_init()
    seed_stock_from_menu()
    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = SQLiteStorage()
    try:
        purged = storage.purge_expired()
        if purged:
            logger.info("FSM: удалено устаревших диалогов: %d", purged)
    except Exception:
        logger.exception("Не удалось почистить устаревшие диалоги")
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    # Проверяем токен реально, до старта поллинга
    me = await bot.get_me()
    logger.info("Бот запущен: @%s (id=%s)", me.username, me.id)

    # --- Меню команд Telegram (по ним кликают в поле ввода) ---
    try:
        await _register_commands(bot)
    except Exception:
        logger.exception("Не удалось зарегистрировать меню команд")

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Polling запущен — бот должен отвечать на /start")
    # Страховка: исключение в хендлере не должно убивать бота насовсем
    while True:
        try:
            await dp.start_polling(bot)
            break
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.exception("Поллинг упал с ошибкой — перезапуск через 3 сек…")
            await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено")
