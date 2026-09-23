import asyncio
import os
from datetime import date, timedelta, datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ozon_perf import (
    get_campaigns,
    get_daily_stats,
    activate_campaign,
    deactivate_campaign,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

PAGE_SIZE = 8
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def only_admin(msg) -> bool:
    return msg.from_user.id == ADMIN_ID


# ---------- ПАРСИНГ ----------
def parse_money(s) -> float:
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0


def parse_int(s) -> int:
    try:
        return int(parse_money(s))
    except Exception:
        return 0


def parse_ozon_date(s: str):
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        if "." in s2:
            head, tail = s2.split(".", 1)
            frac, _, rest = tail.partition("+")
            frac = frac[:6]
            s2 = f"{head}.{frac}+{rest}" if rest else f"{head}.{frac}"
        return datetime.fromisoformat(s2)
    except Exception:
        return None


def campaign_priority(c: dict, now: datetime):
    state = c.get("state")
    updated = parse_ozon_date(c.get("updatedAt") or c.get("createdAt") or "")
    fresh_ts = updated.timestamp() if updated else 0

    if state == "CAMPAIGN_STATE_RUNNING":
        group = 0
    elif updated and (now - updated) <= timedelta(days=30):
        group = 1
    else:
        group = 2
    return (group, -fresh_ts)


# ---------- АГРЕГАЦИЯ ----------
def aggregate_daily(rows: list) -> dict:
    agg: dict = {}
    for r in rows:
        cid = str(r.get("id", "?"))
        name = r.get("title") or cid
        if cid not in agg:
            agg[cid] = {
                "name": name,
                "expense": 0.0,
                "sales": 0.0,
                "orders": 0,
                "clicks": 0,
                "views": 0,
            }
        agg[cid]["expense"] += parse_money(r.get("moneySpent"))
        agg[cid]["sales"] += parse_money(r.get("ordersMoney"))
        agg[cid]["orders"] += parse_int(r.get("orders"))
        agg[cid]["clicks"] += parse_int(r.get("clicks"))
        agg[cid]["views"] += parse_int(r.get("views"))
    return agg


# ---------- ОТЧЁТ ----------
def format_daily_report(rows: list, title: str) -> str:
    if not rows:
        return f"📊 <b>{title}</b>\n\nЗа этот период данных нет."

    agg = aggregate_daily(rows)
    total_expense = sum(v["expense"] for v in agg.values())
    total_sales = sum(v["sales"] for v in agg.values())
    total_orders = sum(v["orders"] for v in agg.values())
    total_clicks = sum(v["clicks"] for v in agg.values())
    total_views = sum(v["views"] for v in agg.values())

    drr = (total_expense / total_sales * 100) if total_sales > 0 else 0
    cpc = (total_expense / total_clicks) if total_clicks > 0 else 0

    dates = sorted({r.get("date", "") for r in rows if r.get("date")})
    period = f"{dates[0]} — {dates[-1]}" if dates else "?"

    lines = [
        f"📊 <b>{title}</b>",
        f"Период: {period}",
        "",
        f"💰 Расход: <b>{total_expense:,.2f} ₽</b>",
        f"📈 Выручка: <b>{total_sales:,.2f} ₽</b>",
        f"🛒 Заказов: {total_orders}",
        f"👁 Показов: {total_views}",
        f"🖱 Кликов: {total_clicks}",
        f"📉 ДРР: <b>{drr:.1f}%</b>",
        f"💵 CPC: {cpc:,.2f} ₽",
        "",
        "<b>По кампаниям:</b>",
    ]
    for cid, info in sorted(agg.items(), key=lambda x: -x[1]["expense"]):
        lines.append(
            f"• {info['name']} (ID: {cid}): "
            f"{info['expense']:,.2f} ₽ · {info['orders']} зак."
        )
    return "\n".join(lines)


# ---------- КЛАВИАТУРА СПИСКА КАМПАНИЙ ----------
async def build_campaigns_keyboard(mode: str, page: int):
    """
    mode: 'cpc' — только оплата за клик, 'all' — все.
    Расход — за СЕГОДНЯ (МСК), в шапке — общий расход дня.
    """
    try:
        campaigns = await get_campaigns()
    except Exception as e:
        return f"❌ Ошибка получения кампаний: {e}", None

    # Данные за сегодня
    today_str = datetime.now(MOSCOW_TZ).date().isoformat()
    try:
        rows = await get_daily_stats(today_str, today_str)
    except Exception:
        rows = []

    agg = aggregate_daily(rows)
    expense_by_campaign = {cid: v["expense"] for cid, v in agg.items()}

    # --- ОБЩИЙ РАСХОД ЗА ДЕНЬ ---
    total_expense_today = sum(v["expense"] for v in agg.values())
    total_orders_today = sum(v["orders"] for v in agg.values())
    total_sales_today = sum(v["sales"] for v in agg.values())
    drr_today = (total_expense_today / total_sales_today * 100) if total_sales_today > 0 else 0

    # Фильтр
    if mode == "cpc":
        filtered = [c for c in campaigns if c.get("PaymentType") == "CPC"]
    else:
        filtered = list(campaigns)

    # Сортировка
    now = datetime.now(timezone.utc)
    filtered.sort(key=lambda c: campaign_priority(c, now))

    total_pages = max(1, (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = filtered[start:start + PAGE_SIZE]

    buttons = []
    for c in chunk:
        cid = str(c.get("id"))
        title = c.get("title") or c.get("advObjectType") or "Кампания"
        if len(title) > 20:
            title = title[:17] + "..."

        spent = expense_by_campaign.get(cid, 0.0)
        state = c.get("state")

        if state == "CAMPAIGN_STATE_RUNNING":
            icon = "🟢"
            action = "off"
            hint = "⏹"
        else:
            updated = parse_ozon_date(c.get("updatedAt") or c.get("createdAt") or "")
            if updated and (now - updated) <= timedelta(days=30):
                icon = "🟡"
            else:
                icon = "⚪"
            action = "on"
            hint = "▶️"

        text = f"{hint}{icon} {title} — {spent:,.2f} ₽"
        buttons.append([
            InlineKeyboardButton(text=text[:64], callback_data=f"{action}:{cid}")
        ])

    # Навигация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"pg:{mode}:{page-1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"pg:{mode}:{page+1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    buttons.append(nav)

    if mode == "cpc":
        buttons.append([InlineKeyboardButton(text="📋 Показать все", callback_data="pg:all:0")])
    else:
        buttons.append([InlineKeyboardButton(text="💳 Только оплата за клик", callback_data="pg:cpc:0")])

    label = "оплата за клик (CPC)" if mode == "cpc" else "все"
    text = (
        f"📊 <b>Расход за сегодня ({today_str}, МСК): "
        f"{total_expense_today:,.2f} ₽</b>\n"
        f"🛒 Заказов: {total_orders_today} · "
        f"📈 Выручка: {total_sales_today:,.2f} ₽ · "
        f"📉 ДРР: {drr_today:.1f}%\n"
        f"──────────────\n"
        f"📋 <b>Кампании</b> ({label}) — найдено <b>{len(filtered)}</b>, "
        f"страница {page+1}/{total_pages}\n\n"
        f"▶️ — включить · ⏹ — выключить\n"
        f"🟢 активные · 🟡 за последний месяц · ⚪ архив"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- КОМАНДЫ ----------
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not only_admin(msg):
        return
    await msg.answer(
        "Привет! Я слежу за рекламными расходами Ozon.\n\n"
        "Команды:\n"
        "/today — расходы за сегодня (МСК)\n"
        "/week — расходы за 7 дней\n"
        "/campaigns — список кампаний: включить / выключить"
    )


@dp.message(Command("today"))
async def cmd_today(msg: Message):
    if not only_admin(msg):
        return
    try:
        today = datetime.now(MOSCOW_TZ).date().isoformat()
        rows = await get_daily_stats(today, today)
    except Exception as e:
        await msg.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
        return
    title = f"Расходы за сегодня ({datetime.now(MOSCOW_TZ).date().isoformat()}, МСК)"
    await msg.answer(format_daily_report(rows, title), parse_mode="HTML")


@dp.message(Command("week"))
async def cmd_week(msg: Message):
    if not only_admin(msg):
        return
    date_to_msk = datetime.now(MOSCOW_TZ).date()
    date_from_msk = date_to_msk - timedelta(days=6)
    try:
        rows = await get_daily_stats(date_from_msk.isoformat(), date_to_msk.isoformat())
    except Exception as e:
        await msg.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
        return
    await msg.answer(format_daily_report(rows, "Расходы за 7 дней (МСК)"), parse_mode="HTML")


@dp.message(Command("campaigns"))
async def cmd_campaigns(msg: Message):
    if not only_admin(msg):
        return
    text, kb = await build_campaigns_keyboard("cpc", 0)
    if kb is None:
        await msg.answer(text)
    else:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ---------- КНОПКИ ----------
@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@dp.callback_query(F.data.startswith("pg:"))
async def cb_paginate(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        _, mode, page_str = cb.data.split(":")
        page = int(page_str)
    except Exception:
        mode, page = "cpc", 0

    text, kb = await build_campaigns_keyboard(mode, page)
    if kb is None:
        await cb.answer(text, show_alert=True)
        return
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("off:"))
async def cb_off(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа", show_alert=True)
        return
    cid = cb.data.split(":", 1)[1]
    try:
        await deactivate_campaign(int(cid))
        await cb.answer(f"⏹ Кампания {cid} выключена", show_alert=True)
        text, kb = await build_campaigns_keyboard("cpc", 0)
        if kb:
            try:
                await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        await cb.answer(f"❌ Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data.startswith("on:"))
async def cb_on(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа", show_alert=True)
        return
    cid = cb.data.split(":", 1)[1]
    try:
        await activate_campaign(int(cid))
        await cb.answer(f"▶️ Кампания {cid} включена", show_alert=True)
        text, kb = await build_campaigns_keyboard("cpc", 0)
        if kb:
            try:
                await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        await cb.answer(f"❌ Ошибка: {e}", show_alert=True)


# ---------- ЕЖЕЧАСНЫЙ ОТЧЁТ ----------
async def hourly_report():
    try:
        date_to_msk = datetime.now(MOSCOW_TZ).date()
        date_from_msk = date_to_msk - timedelta(days=6)
        rows = await get_daily_stats(date_from_msk.isoformat(), date_to_msk.isoformat())
        text = format_daily_report(rows, "⏰ Ежечасный отчёт (7 дней, МСК)")
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        await bot.send_message(
            ADMIN_ID, f"⚠️ Ошибка отчёта: <code>{e}</code>", parse_mode="HTML"
        )


# ---------- ЗАПУСК ----------
async def main():
    scheduler.add_job(hourly_report, "interval", hours=1)
    scheduler.start()
    print("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())