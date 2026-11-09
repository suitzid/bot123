"""Telegram-бот менеджер чата (один файл). Запуск: python bot.py"""
import os
from dotenv import load_dotenv
import html
import logging
import re
from datetime import datetime, timezone
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import pathlib
import asyncpg
import copy
import json
import asyncio
from datetime import timedelta
import time
from aiogram.types import ChatPermissions
from collections import defaultdict, deque
import random
from aiogram.filters import CommandObject
from aiogram.types import Message
from aiogram import Bot, F, Router
from aiogram.types import ChatMemberUpdated, Message
from aiogram.types import CallbackQuery, ChatMemberUpdated, ContentType, Message
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from urllib.parse import urlparse
from datetime import timezone
from aiogram.types import CallbackQuery
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types.error_event import ErrorEvent
from aiohttp import web

SCHEMA = '''
create table if not exists settings (
    key text primary key,
    value jsonb not null
);
create table if not exists admins (
    user_id bigint primary key,
    added_by bigint,
    added_at timestamptz not null default now()
);
create table if not exists users (
    user_id bigint primary key,
    username text,
    full_name text,
    joined_at timestamptz not null default now(),
    status text not null default 'old'
);
create index if not exists users_username_idx on users (lower(username));
create table if not exists pending_captcha (
    user_id bigint primary key,
    answer int,
    attempts_left int not null,
    expires_at timestamptz not null,
    chat_message_id bigint,
    stage text not null default 'new'
);
create index if not exists pending_expires_idx on pending_captcha (expires_at);
create table if not exists warns (
    id bigserial primary key,
    user_id bigint not null,
    by_admin bigint,
    reason text,
    created_at timestamptz not null default now()
);
create index if not exists warns_user_idx on warns (user_id);
create table if not exists punishments (
    user_id bigint not null,
    type text not null,
    until timestamptz,
    reason text,
    by_admin bigint,
    created_at timestamptz not null default now(),
    primary key (user_id, type)
);
create table if not exists scheduled_deletes (
    id bigserial primary key,
    chat_id bigint not null,
    message_id bigint not null,
    delete_at timestamptz not null
);
create index if not exists sched_delete_idx on scheduled_deletes (delete_at);
create table if not exists logs (
    id bigserial primary key,
    ts timestamptz not null default now(),
    actor bigint,
    action text not null,
    target bigint,
    details text
);
create index if not exists logs_ts_idx on logs (ts desc);
'''


# ======== config.py ========
load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise SystemExit(f"Не задана переменная окружения {name}")
    return v


BOT_TOKEN = _req("BOT_TOKEN")
CHAT_ID = int(_req("CHAT_ID"))
SUPER_ADMINS = {int(x) for x in _req("SUPER_ADMINS").replace(" ", "").split(",") if x}
DATABASE_URL = _req("DATABASE_URL")
PORT = int(os.getenv("PORT", "8080"))
BUTTON_STYLE = os.getenv("BUTTON_STYLE", "auto").lower()


# ======== utils/helpers.py ========
log = logging.getLogger(__name__)
esc = html.escape


def now() -> datetime:
    return datetime.now(timezone.utc)


def mention(uid: int, name: str | None = None) -> str:
    return f'<a href="tg://user?id={uid}">{esc(name or str(uid))}</a>'


_DUR = re.compile(r"^(\d+)\s*([smhdw]?)$", re.I)
_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(s: str, bare: str = "s") -> int | None:
    m = _DUR.match((s or "").strip())
    if not m:
        return None
    unit = (m.group(2) or bare).lower()
    return int(m.group(1)) * _MULT[unit]


def fmt_duration(sec: int | None) -> str:
    if not sec:
        return "навсегда"
    parts = []
    for name, div in (("д", 86400), ("ч", 3600), ("мин", 60), ("с", 1)):
        q, sec = divmod(sec, div)
        if q:
            parts.append(f"{q} {name}")
    return " ".join(parts) or "0 с"


def fmt_dt(dt) -> str:
    if not dt:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


async def safe_send(bot: Bot, chat_id: int, text: str, **kw):
    try:
        return await bot.send_message(chat_id, text, **kw)
    except TelegramAPIError as e:
        msg = str(e).lower()
        if "parse" in msg or "entities" in msg:
            try:
                return await bot.send_message(chat_id, text, parse_mode=None, **kw)
            except TelegramAPIError as e2:
                log.warning("send to %s failed: %s", chat_id, e2)
        else:
            log.warning("send to %s failed: %s", chat_id, e)
    return None


async def safe_delete(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


# ======== keyboards/kb.py ========
# style: primary (синий), success (зелёный), danger (красный) — если поддерживается библиотекой/API
STYLE_OK = BUTTON_STYLE == "auto" and "style" in InlineKeyboardButton.model_fields
_EMO = {"primary": "🔵", "success": "🟢", "danger": "🔴"}


def btn(text: str, cb: str | None = None, url: str | None = None, style: str | None = None):
    kw = {"text": text}
    if cb is not None:
        kw["callback_data"] = cb
    if url:
        kw["url"] = url
    if style and BUTTON_STYLE != "off":
        if STYLE_OK:
            kw["style"] = style
        else:
            kw["text"] = f"{_EMO.get(style, '')} {text}".strip()
    return InlineKeyboardButton(**kw)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


# ======== db/database.py ========
db_pool: asyncpg.Pool | None = None


async def db_init(dsn: str) -> None:
    global db_pool
    # statement_cache_size=0 обязателен для Supabase pooler (pgbouncer, transaction mode)
    db_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, statement_cache_size=0)
    schema = SCHEMA
    async with db_pool.acquire() as c:
        await c.execute(schema)


async def db_fetch(q, *a):
    return await db_pool.fetch(q, *a)


async def db_fetchrow(q, *a):
    return await db_pool.fetchrow(q, *a)


async def db_fetchval(q, *a):
    return await db_pool.fetchval(q, *a)


async def db_execute(q, *a):
    return await db_pool.execute(q, *a)


# ======== services/settings.py ========
settings_DEFAULTS = {
    "welcome_text": "",
    "rules_text": "",
    "sub_enabled": False,
    "sub_channel": 0,
    "sub_channel_title": "",
    "sub_channel_link": "",
    "captcha_time": 300,
    "captcha_attempts": 3,
    "warn_limit": 3,
    "warn_action": "mute",
    "warn_duration": 86400,
    "warn_reset": True,
    "default_mute": 3600,
    "cmd_cleanup": 30,
    "antispam_on": False,
    "antispam_repeats": 3,
    "antispam_window": 30,
    "antispam_action": "mute",
    "antispam_duration": 600,
    "antiflood_on": False,
    "flood_msgs": 6,
    "flood_secs": 5,
    "flood_duration": 600,
    "antiraid_on": False,
    "raid_threshold": 8,
    "raid_duration": 900,
    "raid_captcha_time": 120,
    "raid_attempts": 1,
    "raid_block": False,
    "filter_links_on": False,
    "links_scope": "newbies",
    "newbie_hours": 24,
    "link_whitelist": [],
    "filter_words_on": False,
    "stop_words": [],
    "filter_action": "delete",
    "filter_duration": 600,
    "notify_captcha": True,
}

settings__cache: dict = {}


async def settings_load() -> None:
    rows = await db_fetch("select key, value from settings")
    settings__cache.clear()
    for r in rows:
        v = r["value"]
        settings__cache[r["key"]] = json.loads(v) if isinstance(v, str) else v


def settings_get(key: str):
    return copy.deepcopy(settings__cache.get(key, settings_DEFAULTS[key]))


async def settings_put(key: str, value) -> None:
    await db_execute(
        "insert into settings(key, value) values($1, $2::jsonb) "
        "on conflict (key) do update set value = excluded.value",
        key, json.dumps(value, ensure_ascii=False),
    )
    settings__cache[key] = value


# ======== services/roles.py ========
roles__admins: set[int] = set()


async def roles_load() -> None:
    rows = await db_fetch("select user_id from admins")
    roles__admins.clear()
    roles__admins.update(r["user_id"] for r in rows)


def roles_is_super(uid: int) -> bool:
    return uid in SUPER_ADMINS


def roles_is_admin(uid: int) -> bool:
    return uid in SUPER_ADMINS or uid in roles__admins


def roles_admin_ids() -> set[int]:
    return set(roles__admins)


async def roles_add(uid: int, by: int) -> None:
    await db_execute(
        "insert into admins(user_id, added_by) values($1,$2) on conflict do nothing", uid, by
    )
    roles__admins.add(uid)


async def roles_remove(uid: int) -> None:
    await db_execute("delete from admins where user_id=$1", uid)
    roles__admins.discard(uid)


# ======== services/users.py ========
users__seen: dict[int, tuple] = {}


async def users_touch(user, status: str | None = None, joined: bool = False) -> None:
    """Запоминает пользователя. joined=True — зафиксировать время входа."""
    key = (user.username, user.full_name)
    if status is None and not joined and users__seen.get(user.id) == key:
        return
    users__seen[user.id] = key
    await db_execute(
        """
        insert into users(user_id, username, full_name, joined_at, status)
        values($1, $2, $3,
               case when $4::boolean then now() else timestamptz '2000-01-01' end,
               coalesce($5::text, 'old'))
        on conflict (user_id) do update set
            username = excluded.username,
            full_name = excluded.full_name,
            status = coalesce($5::text, users_status),
            joined_at = case when $4::boolean then now() else users_joined_at end
        """,
        user.id, user.username, user.full_name, joined, status,
    )


async def users_set_status(uid: int, status: str) -> None:
    await db_execute("update users set status=$2 where user_id=$1", uid, status)


async def users_name(uid: int) -> str:
    n = await db_fetchval("select full_name from users where user_id=$1", uid)
    return n or str(uid)


async def users_find_by_username(uname: str) -> int | None:
    return await db_fetchval(
        "select user_id from users where lower(username)=lower($1)", uname.lstrip("@")
    )


async def users_get(uid: int):
    return await db_fetchrow("select * from users where user_id=$1", uid)


async def users_is_newbie(uid: int, hours: int) -> bool:
    v = await db_fetchval(
        "select joined_at > now() - make_interval(hours => $2::int) from users where user_id=$1",
        uid, hours,
    )
    return bool(v)


# ======== services/logs.py ========
logs_log = logging.getLogger(__name__)
logs__tasks: set = set()

logs_TITLES = {
    "warn": "⚠️ Предупреждение", "unwarn": "♻️ Снято предупреждение", "mute": "🔇 Мут",
    "unmute": "🔊 Размут", "ban": "🚫 Бан", "unban": "✅ Разбан", "kick": "👢 Кик",
    "captcha_ok": "✅ Капча пройдена", "captcha_fail": "❌ Капча провалена",
    "raid_mode": "🛡 Антирейд", "setting": "⚙️ Изменена настройка",
    "admin_add": "👥 Добавлен админ", "admin_del": "👥 Снят админ",
    "auto_флуд": "⚡ Антифлуд", "auto_спам": "🔁 Антиспам", "auto_ссылка": "🧹 Фильтр ссылок",
    "auto_стоп-слово": "🧹 Фильтр слов", "foreign_chat": "🚪 Бот вышел из чужого чата",
}


async def logs__label(bot: Bot, uid) -> str:
    if not uid or uid == bot.id:
        return "🤖 Бот"
    n = await db_fetchval("select full_name from users where user_id=$1", uid)
    return f"{mention(uid, n)} (<code>{uid}</code>)"


async def logs__notify(bot: Bot, actor, action, target, details):
    text = f"📝 <b>{logs_TITLES.get(action, esc(action))}</b>\n👤 Кто: {await logs__label(bot, actor)}"
    if target:
        text += f"\n🎯 Кому: {await logs__label(bot, target)}"
    if details:
        text += f"\n💬 {esc(details)[:800]}"
    for sid in SUPER_ADMINS:
        try:
            await bot.send_message(sid, text, disable_notification=True)
        except TelegramAPIError as e:
            logs_log.warning("log to %s failed: %s", sid, e)


async def logs_log_event(bot: Bot, actor, action: str, target=None, details: str = "", notify: bool = True):
    if actor == bot.id:
        actor = 0
    try:
        await db_execute(
            "insert into logs(actor, action, target, details) values($1,$2,$3,$4)",
            actor, action, target, details or "",
        )
    except Exception:
        logs_log.exception("log insert failed")
    logs_log.info("LOG %s actor=%s target=%s %s", action, actor, target, details)
    if notify:
        t = asyncio.create_task(logs__notify(bot, actor, action, target, details))
        logs__tasks.add(t)
        t.add_done_callback(logs__tasks.discard)


async def logs_recent(n: int = 15):
    return await db_fetch("select * from logs order by id desc limit $1", n)


async def logs_stats():
    return await db_fetch(
        "select action, count(*) c from logs where ts > now() - interval '24 hours' "
        "group by action order by c desc limit 12"
    )


# ======== services/scheduler.py ========
scheduler_log = logging.getLogger(__name__)


async def scheduler_schedule_delete(chat_id: int, message_id: int, seconds: int) -> None:
    await db_execute(
        "insert into scheduled_deletes(chat_id, message_id, delete_at) values($1,$2,$3)",
        chat_id, message_id, now() + timedelta(seconds=seconds),
    )


async def scheduler_tick(bot: Bot) -> None:

    rows = await db_fetch(
        "delete from scheduled_deletes where delete_at <= now() returning chat_id, message_id"
    )
    for r in rows:
        await safe_delete(bot, r["chat_id"], r["message_id"])
    expired = await db_fetch("select user_id from pending_captcha where expires_at <= now()")
    for r in expired:
        await captcha_fail(bot, r["user_id"], "время вышло")


async def scheduler_run(bot: Bot) -> None:
    while True:
        try:
            await scheduler_tick(bot)
        except Exception:
            scheduler_log.exception("scheduler tick failed")
        await asyncio.sleep(3)


# ======== services/moderation.py ========
moderation__ALL = [
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews", "can_invite_users",
    "can_change_info", "can_pin_messages", "can_manage_topics",
]
moderation_NO_PERMS = ChatPermissions(**{k: False for k in moderation__ALL})
moderation_FULL_PERMS = ChatPermissions(**{k: True for k in moderation__ALL})
moderation__cache: tuple = (0.0, None)


async def moderation_chat_perms(bot: Bot) -> ChatPermissions:
    """Права по умолчанию в чате (кэш 5 минут)."""
    global moderation__cache
    t = time.time()
    if moderation__cache[1] is None or t - moderation__cache[0] > 300:
        try:
            ch = await bot.get_chat(CHAT_ID)
            if ch.permissions:
                moderation__cache = (t, ch.permissions)
        except TelegramAPIError:
            pass
    return moderation__cache[1] or moderation_FULL_PERMS


def moderation_check_target(actor: int, target: int, bot_id: int) -> str | None:
    if target == bot_id:
        return "Нельзя применить к боту."
    if target == actor:
        return "Нельзя применить к себе."
    if roles_is_super(target):
        return "Главных админов трогать нельзя."
    if roles_is_admin(target) and not roles_is_super(actor):
        return "Нельзя наказывать администратора."
    return None


async def moderation__save(kind: str, uid: int, until, reason: str, by: int) -> None:
    await db_execute(
        """insert into punishments(user_id, type, until, reason, by_admin)
           values($1,$2,$3,$4,$5)
           on conflict (user_id, type) do update set
             until=excluded.until, reason=excluded.reason,
             by_admin=excluded.by_admin, created_at=now()""",
        uid, kind, until, reason or "", by,
    )


async def moderation_mute(bot: Bot, uid: int, seconds: int, reason: str, by: int):
    """seconds=0 — навсегда. Минимум 60 секунд (ограничение Telegram)."""
    until = None
    if seconds:
        until = now() + timedelta(seconds=max(seconds, 60))
    await bot.restrict_chat_member(CHAT_ID, uid, permissions=moderation_NO_PERMS, until_date=until)
    await moderation__save("mute", uid, until, reason, by)
    return until


async def moderation_unmute(bot: Bot, uid: int) -> None:
    await bot.restrict_chat_member(CHAT_ID, uid, permissions=await moderation_chat_perms(bot))
    await db_execute("delete from punishments where user_id=$1 and type='mute'", uid)


async def moderation_ban(bot: Bot, uid: int, seconds: int, reason: str, by: int):
    until = now() + timedelta(seconds=max(seconds, 60)) if seconds else None
    await bot.ban_chat_member(CHAT_ID, uid, until_date=until)
    await moderation__save("ban", uid, until, reason, by)
    return until


async def moderation_unban(bot: Bot, uid: int) -> None:
    await bot.unban_chat_member(CHAT_ID, uid, only_if_banned=True)
    await db_execute("delete from punishments where user_id=$1 and type='ban'", uid)


async def moderation_kick(bot: Bot, uid: int) -> None:
    """Кик без бана: пользователь может зайти снова."""
    await bot.ban_chat_member(CHAT_ID, uid)
    await bot.unban_chat_member(CHAT_ID, uid, only_if_banned=True)


async def moderation_warn_count(uid: int) -> int:
    return await db_fetchval("select count(*) from warns where user_id=$1", uid)


async def moderation_warns_list(uid: int):
    return await db_fetch(
        "select by_admin, reason, created_at from warns where user_id=$1 order by id", uid
    )


async def moderation_remove_warn(uid: int) -> bool:
    r = await db_fetchval(
        "delete from warns where id = (select id from warns where user_id=$1 order by id desc limit 1) returning id",
        uid,
    )
    return r is not None


def moderation_punish_text(action: str, dur: int) -> str:
    return f"{'Мут' if action == 'mute' else 'Бан'}: {fmt_duration(dur)}"


async def moderation_add_warn(bot: Bot, uid: int, by: int, reason: str):
    """Возвращает (кол-во варнов, лимит, описание наказания или None)."""
    await db_execute("insert into warns(user_id, by_admin, reason) values($1,$2,$3)", uid, by, reason or "")
    cnt = await moderation_warn_count(uid)
    limit = settings_get("warn_limit")
    pun = None
    if cnt >= limit:
        act, dur = settings_get("warn_action"), settings_get("warn_duration")
        if act == "ban":
            await moderation_ban(bot, uid, dur, f"лимит варнов ({cnt})", by)
        else:
            await moderation_mute(bot, uid, dur, f"лимит варнов ({cnt})", by)
        if settings_get("warn_reset"):
            await db_execute("delete from warns where user_id=$1", uid)
        pun = moderation_punish_text(act, dur)
    return cnt, limit, pun


async def moderation_apply_action(bot: Bot, uid: int, action: str, dur: int, reason: str, by: int) -> str | None:
    """Автонаказание (антиспам, фильтры). delete — ничего кроме удаления сообщения."""
    if action == "warn":
        cnt, limit, pun = await moderation_add_warn(bot, uid, by, reason)
        return f"Предупреждение {cnt}/{limit}" + (f". {pun}" if pun else "")
    if action == "mute":
        await moderation_mute(bot, uid, dur, reason, by)
        return moderation_punish_text("mute", dur)
    if action == "ban":
        await moderation_ban(bot, uid, dur, reason, by)
        return moderation_punish_text("ban", dur)
    return None


# ======== services/protection.py ========
protection__joins: deque = deque()
protection__raid_until = 0.0
protection__hist: dict = defaultdict(deque)
protection__flood: dict = defaultdict(deque)


async def protection_register_join(bot: Bot) -> bool:
    """Фиксирует вход. Возвращает True, если активен режим антирейда."""
    global protection__raid_until
    t = time.time()
    protection__joins.append(t)
    while protection__joins and t - protection__joins[0] > 60:
        protection__joins.popleft()
    if not settings_get("antiraid_on"):
        return False
    if t >= protection__raid_until and len(protection__joins) >= settings_get("raid_threshold"):
        dur = settings_get("raid_duration")
        protection__raid_until = t + dur
        await logs_log_event(
            bot, 0, "raid_mode", None,
            f"{len(protection__joins)} входов за минуту. Усиленная проверка на {fmt_duration(dur)}.",
        )
    return t < protection__raid_until


def protection_check_flood(uid: int) -> bool:
    if not settings_get("antiflood_on"):
        return False
    t = time.time()
    q = protection__flood[uid]
    q.append(t)
    secs = settings_get("flood_secs")
    while q and t - q[0] > secs:
        q.popleft()
    if len(q) >= settings_get("flood_msgs"):
        q.clear()
        return True
    return False


def protection_check_spam(uid: int, key: str) -> bool:
    if not settings_get("antispam_on"):
        return False
    t = time.time()
    q = protection__hist[uid]
    q.append((t, key))
    window = settings_get("antispam_window")
    while q and t - q[0][0] > window:
        q.popleft()
    if sum(1 for _, k in q if k == key) >= settings_get("antispam_repeats"):
        q.clear()
        return True
    return False


# ======== services/captcha.py ========
captcha_log = logging.getLogger(__name__)
captcha_DEFAULT_WELCOME = "👋 Добро пожаловать, {user}!"


def captcha_make_question():
    op = random.choice("+-*")
    if op == "+":
        a, b = random.randint(2, 30), random.randint(2, 30)
        ans = a + b
    elif op == "-":
        a = random.randint(10, 40)
        b = random.randint(2, a - 1)
        ans = a - b
    else:
        a, b = random.randint(2, 9), random.randint(2, 9)
        ans = a * b
    text = f"{a} {'×' if op == '*' else op} {b}"
    opts = {ans}
    while len(opts) < 4:
        v = ans + random.choice([-1, 1]) * random.randint(1, 6)
        if v >= 0:
            opts.add(v)
    opts = list(opts)
    random.shuffle(opts)
    return text, ans, opts


async def captcha_is_subscribed(bot: Bot, uid: int) -> bool:
    ch = settings_get("sub_channel")
    if not ch:
        return True
    try:
        m = await bot.get_chat_member(ch, uid)
    except TelegramAPIError as e:
        captcha_log.warning("subscription check failed: %s", e)
        return True  # не блокируем людей, если бот не может проверить
    if m.status in ("member", "administrator", "creator"):
        return True
    return m.status == "restricted" and bool(getattr(m, "is_member", False))


async def captcha_success(bot: Bot, uid: int) -> bool:
    row = await db_fetchrow(
        "delete from pending_captcha where user_id=$1 returning chat_message_id", uid
    )
    if not row:
        return False
    try:
        await bot.restrict_chat_member(CHAT_ID, uid, permissions=await moderation_chat_perms(bot))
    except TelegramAPIError as e:
        captcha_log.error("unrestrict failed for %s: %s", uid, e)
    await db_execute("update users set status='verified' where user_id=$1", uid)
    await safe_delete(bot, CHAT_ID, row["chat_message_id"])
    name = await users_name(uid)
    tpl = settings_get("welcome_text") or captcha_DEFAULT_WELCOME
    msg = await safe_send(bot, CHAT_ID, tpl.replace("{user}", mention(uid, name)))
    if msg:
        await scheduler_schedule_delete(CHAT_ID, msg.message_id, 60)
    await logs_log_event(bot, uid, "captcha_ok", uid, "", notify=settings_get("notify_captcha"))
    return True


async def captcha_fail(bot: Bot, uid: int, reason: str) -> bool:
    row = await db_fetchrow(
        "delete from pending_captcha where user_id=$1 returning chat_message_id", uid
    )
    if not row:
        return False
    try:
        await moderation_kick(bot, uid)
    except TelegramAPIError as e:
        captcha_log.error("kick failed for %s: %s", uid, e)
    await safe_delete(bot, CHAT_ID, row["chat_message_id"])
    name = await users_name(uid)
    msg = await safe_send(
        bot, CHAT_ID,
        f"❌ {mention(uid, name)} не прошёл проверку ({reason}) и удалён из чата. "
        "Можно зайти снова и пройти её заново.",
    )
    if msg:
        await scheduler_schedule_delete(CHAT_ID, msg.message_id, 60)
    await safe_send(bot, uid, "❌ Проверка не пройдена. Ты можешь зайти в чат снова и пройти её заново.")
    await db_execute("update users set status='kicked' where user_id=$1", uid)
    await logs_log_event(bot, uid, "captcha_fail", uid, reason, notify=settings_get("notify_captcha"))
    return True


# ======== services/listing.py ========
listing_PAGE = 10


async def listing_render_list(kind: str, page: int = 0, back: bool = False):
    where = "p.type=$1 and (p.until is null or p.until>now())"
    total = await db_fetchval(f"select count(*) from punishments p where {where}", kind)
    pages = max(1, (total + listing_PAGE - 1) // listing_PAGE)
    page = min(max(page, 0), pages - 1)
    rows = await db_fetch(
        f"""select p.user_id, p.until, p.reason, u.full_name, u.username
            from punishments p left join users u on u.user_id=p.user_id
            where {where} order by p.created_at desc offset $2 limit $3""",
        kind, page * listing_PAGE, listing_PAGE,
    )
    title = "🚫 Забаненные" if kind == "ban" else "🔇 Замученные"
    lines = [f"<b>{title}</b> ({total})", ""]
    if not rows:
        lines.append("Список пуст.")
    for r in rows:
        name = esc(r["full_name"] or str(r["user_id"]))
        uname = f" @{esc(r['username'])}" if r["username"] else ""
        until = "навсегда" if r["until"] is None else "до " + fmt_dt(r["until"])
        reason = f"\n   └ {esc(r['reason'])}" if r["reason"] else ""
        lines.append(f"• {name}{uname} (<code>{r['user_id']}</code>) — {until}{reason}")
    nav = []
    if page > 0:
        nav.append(btn("⬅️", cb=f"lst:{kind}:{page - 1}", style="primary"))
    nav.append(btn(f"🔄 {page + 1}/{pages}", cb=f"lst:{kind}:{page}", style="success"))
    if page < pages - 1:
        nav.append(btn("➡️", cb=f"lst:{kind}:{page + 1}", style="primary"))
    rows_kb = [nav]
    if back:
        rows_kb.append([btn("⬅️ Назад", cb="menu:home", style="primary")])
    return "\n".join(lines), kb(*rows_kb)


# ======== handlers/common.py ========
async def tmp(bot: Bot, text: str, secs: int | None = None, **kw):
    """Сообщение в чат, которое удалится само."""
    msg = await safe_send(bot, CHAT_ID, text, disable_notification=True, **kw)
    if msg:
        await scheduler_schedule_delete(CHAT_ID, msg.message_id, secs or settings_get("cmd_cleanup"))
    return msg


async def resolve(m: Message, command: CommandObject, bot: Bot):
    """Цель команды: ответ на сообщение / @username / ID. Возвращает (uid, остаток аргументов) или None."""
    args = (command.args or "").split()
    rp = m.reply_to_message
    if rp and rp.from_user:
        await users_touch(rp.from_user)
        return rp.from_user.id, args
    if args:
        a = args[0]
        if a.lstrip("-").isdigit():
            return int(a), args[1:]
        if a.startswith("@"):
            uid = await users_find_by_username(a)
            if uid:
                return uid, args[1:]
    return None


# ======== handlers/guard.py ========
log = logging.getLogger(__name__)
router_guard = Router()


async def _leave(bot: Bot, chat) -> None:
    try:
        await bot.leave_chat(chat.id)
    except Exception as e:
        log.warning("leave_chat %s failed: %s", chat.id, e)
    await logs_log_event(bot, 0, "foreign_chat", None, f"{chat.title} ({chat.id})")


@router_guard.my_chat_member()
async def on_my_member(event: ChatMemberUpdated, bot: Bot):
    chat = event.chat
    # каналы (в т.ч. канал подписки) не трогаем — только группы
    if chat.id == CHAT_ID or chat.type not in ("group", "supergroup"):
        return
    if event.new_chat_member.status in ("member", "administrator", "restricted"):
        await _leave(bot, chat)


@router_guard.message(F.chat.type.in_({"group", "supergroup"}), F.chat.id != CHAT_ID)
async def foreign_message(m: Message, bot: Bot):
    await _leave(bot, m.chat)


# ======== handlers/chat_events.py ========
log = logging.getLogger(__name__)
router_chat_events = Router()
router_chat_events.chat_member.filter(F.chat.id == CHAT_ID)
router_chat_events.message.filter(F.chat.id == CHAT_ID)
router_chat_events.callback_query.filter(F.message.chat.id == CHAT_ID)

_NAMES = [
    "NEW_CHAT_MEMBERS", "LEFT_CHAT_MEMBER", "PINNED_MESSAGE", "NEW_CHAT_TITLE", "NEW_CHAT_PHOTO",
    "DELETE_CHAT_PHOTO", "GROUP_CHAT_CREATED", "SUPERGROUP_CHAT_CREATED",
    "MESSAGE_AUTO_DELETE_TIMER_CHANGED", "VIDEO_CHAT_STARTED", "VIDEO_CHAT_ENDED",
    "VIDEO_CHAT_PARTICIPANTS_INVITED", "VIDEO_CHAT_SCHEDULED", "FORUM_TOPIC_CREATED",
    "FORUM_TOPIC_EDITED", "FORUM_TOPIC_CLOSED", "FORUM_TOPIC_REOPENED",
]
SERVICE = {getattr(ContentType, n) for n in _NAMES if hasattr(ContentType, n)}


def in_chat(member) -> bool:
    if member.status in ("member", "administrator", "creator"):
        return True
    return member.status == "restricted" and bool(getattr(member, "is_member", False))


@router_chat_events.message(F.content_type.in_(SERVICE))
async def service_messages(m: Message, bot: Bot):
    await safe_delete(bot, m.chat.id, m.message_id)


@router_chat_events.chat_member()
async def on_chat_member(event: ChatMemberUpdated, bot: Bot):
    user = event.new_chat_member.user
    if user.is_bot:
        return
    was, now_in = in_chat(event.old_chat_member), in_chat(event.new_chat_member)
    if not was and now_in:
        await handle_join(bot, user)
    elif was and not now_in:
        await handle_leave(bot, user)


async def handle_join(bot: Bot, user) -> None:
    if roles_is_admin(user.id):
        return
    await users_touch(user, status="pending", joined=True)
    raid = await protection_register_join(bot)
    if raid and settings_get("raid_block"):
        try:
            await moderation_kick(bot, user.id)
        except TelegramAPIError as e:
            log.warning("raid kick failed: %s", e)
        await logs_log_event(bot, 0, "kick", user.id, "антирейд: вход заблокирован", notify=False)
        return
    ctime = settings_get("raid_captcha_time") if raid else settings_get("captcha_time")
    attempts = settings_get("raid_attempts") if raid else settings_get("captcha_attempts")
    try:
        await bot.restrict_chat_member(CHAT_ID, user.id, permissions=moderation_NO_PERMS)
    except TelegramAPIError as e:
        log.error("restrict on join failed (нет прав?): %s", e)
        return
    old = await db_fetchval("select chat_message_id from pending_captcha where user_id=$1", user.id)
    await safe_delete(bot, CHAT_ID, old)
    text = (
        f"👋 {mention(user.id, user.full_name)}, добро пожаловать!\n\n"
        f"Чтобы писать в чате, пройди проверку в боте. На это есть <b>{fmt_duration(ctime)}</b>."
    )
    if raid:
        text += "\n\n🛡 Включён усиленный режим проверки."
    msg = await safe_send(
        bot, CHAT_ID, text,
        reply_markup=kb([btn("🤖 Пройти проверку", cb=f"gate:{user.id}", style="success")]),
    )
    await db_execute(
        """insert into pending_captcha(user_id, answer, attempts_left, expires_at, chat_message_id)
           values($1, 0, $2, $3, $4)
           on conflict (user_id) do update set
             attempts_left=excluded.attempts_left, expires_at=excluded.expires_at,
             chat_message_id=excluded.chat_message_id, answer=0, stage='new'""",
        user.id, attempts, now() + timedelta(seconds=ctime), msg.message_id if msg else None,
    )


async def handle_leave(bot: Bot, user) -> None:
    row = await db_fetchrow(
        "delete from pending_captcha where user_id=$1 returning chat_message_id", user.id
    )
    if row:
        await safe_delete(bot, CHAT_ID, row["chat_message_id"])
        await users_set_status(user.id, "left")


@router_chat_events.callback_query(F.data.startswith("gate:"))
async def gate(cb: CallbackQuery, bot: Bot):
    uid = int(cb.data.split(":")[1])
    if cb.from_user.id != uid:
        await cb.answer("Эта кнопка не для вас.", show_alert=True)
        return
    me = await bot.me()
    await cb.answer(url=f"https://t.me/{me.username}?start=verify")


# ======== handlers/moderation_cmds.py ========
router_moderation_cmds = Router()
router_moderation_cmds.message.filter(F.chat.id == CHAT_ID)


async def _admin_only(m: Message, bot: Bot) -> bool:
    if m.from_user and roles_is_admin(m.from_user.id):
        return True
    await safe_delete(bot, m.chat.id, m.message_id)
    return False


async def _target(m: Message, command: CommandObject, bot: Bot):
    if not await _admin_only(m, bot):
        return None
    r = await resolve(m, command, bot)
    if r is None:
        await safe_delete(bot, m.chat.id, m.message_id)
        await tmp(bot, "❌ Укажи пользователя: ответом на сообщение, @username или ID.")
        return None
    uid, rest = r
    err = moderation_check_target(m.from_user.id, uid, bot.id)
    if err:
        await safe_delete(bot, m.chat.id, m.message_id)
        await tmp(bot, f"❌ {err}")
        return None
    return uid, rest


async def _done(m: Message, bot: Bot, text: str, action: str, uid: int, details: str = ""):
    await safe_delete(bot, m.chat.id, m.message_id)
    await tmp(bot, text)
    await logs_log_event(bot, m.from_user.id, action, uid, details)


async def _fail(m: Message, bot: Bot, e: Exception):
    await safe_delete(bot, m.chat.id, m.message_id)
    await tmp(bot, f"❌ Не удалось: {esc(getattr(e, 'message', None) or str(e))}")


def _dur_reason(rest: list[str], default: int):
    if rest:
        d = parse_duration(rest[0], "m")
        if d is not None:
            return d, " ".join(rest[1:])
    return default, " ".join(rest)


@router_moderation_cmds.message(Command("warn"))
async def c_warn(m: Message, command: CommandObject, bot: Bot):
    t = await _target(m, command, bot)
    if not t:
        return
    uid, rest = t
    reason = " ".join(rest)
    try:
        cnt, limit, pun = await moderation_add_warn(bot, uid, m.from_user.id, reason)
    except TelegramAPIError as e:
        return await _fail(m, bot, e)
    text = f"⚠️ {mention(uid, await users_name(uid))}: предупреждение {cnt}/{limit}"
    if reason:
        text += f"\n📝 {esc(reason)}"
    if pun:
        text += f"\n🔨 Лимит достигнут → {pun}"
    await _done(m, bot, text, "warn", uid, f"{cnt}/{limit} {reason}".strip())


@router_moderation_cmds.message(Command("unwarn"))
async def c_unwarn(m: Message, command: CommandObject, bot: Bot):
    t = await _target(m, command, bot)
    if not t:
        return
    uid, _ = t
    ok = await moderation_remove_warn(uid)
    name = mention(uid, await users_name(uid))
    if not ok:
        await safe_delete(bot, m.chat.id, m.message_id)
        return await tmp(bot, f"ℹ️ У {name} нет предупреждений.")
    cnt = await moderation_warn_count(uid)
    await _done(m, bot, f"♻️ С {name} снято предупреждение ({cnt}/{settings_get('warn_limit')})", "unwarn", uid)


@router_moderation_cmds.message(Command("warns"))
async def c_warns(m: Message, command: CommandObject, bot: Bot):
    t = await _target(m, command, bot)
    if not t:
        return
    uid, _ = t
    rows = await moderation_warns_list(uid)
    name = mention(uid, await users_name(uid))
    lines = [f"⚠️ {name}: {len(rows)}/{settings_get('warn_limit')}"]
    for i, r in enumerate(rows[-10:], 1):
        lines.append(f"{i}. {fmt_dt(r['created_at'])} — {esc(r['reason'] or 'без причины')}")
    await safe_delete(bot, m.chat.id, m.message_id)
    await tmp(bot, "\n".join(lines))


@router_moderation_cmds.message(Command("mute"))
async def c_mute(m: Message, command: CommandObject, bot: Bot):
    t = await _target(m, command, bot)
    if not t:
        return
    uid, rest = t
    secs, reason = _dur_reason(rest, settings_get("default_mute"))
    try:
        await moderation_mute(bot, uid, secs, reason, m.from_user.id)
    except TelegramAPIError as e:
        return await _fail(m, bot, e)
    text = f"🔇 {mention(uid, await users_name(uid))} в муте: {fmt_duration(secs)}"
    if reason:
        text += f"\n📝 {esc(reason)}"
    await _done(m, bot, text, "mute", uid, f"{fmt_duration(secs)} {reason}".strip())


@router_moderation_cmds.message(Command("unmute"))
async def c_unmute(m: Message, command: CommandObject, bot: Bot):
    t = await _target(m, command, bot)
    if not t:
        return
    uid, _ = t
    try:
        await moderation_unmute(bot, uid)
    except TelegramAPIError as e:
        return await _fail(m, bot, e)
    await _done(m, bot, f"🔊 {mention(uid, await users_name(uid))} размучен", "unmute", uid)


@router_moderation_cmds.message(Command("ban"))
async def c_ban(m: Message, command: CommandObject, bot: Bot):
    t = await _target(m, command, bot)
    if not t:
        return
    uid, rest = t
    secs, reason = _dur_reason(rest, 0)
    try:
        await moderation_ban(bot, uid, secs, reason, m.from_user.id)
    except TelegramAPIError as e:
        return await _fail(m, bot, e)
    text = f"🚫 {mention(uid, await users_name(uid))} забанен: {fmt_duration(secs)}"
    if reason:
        text += f"\n📝 {esc(reason)}"
    await _done(m, bot, text, "ban", uid, f"{fmt_duration(secs)} {reason}".strip())


@router_moderation_cmds.message(Command("unban"))
async def c_unban(m: Message, command: CommandObject, bot: Bot):
    t = await _target(m, command, bot)
    if not t:
        return
    uid, _ = t
    try:
        await moderation_unban(bot, uid)
    except TelegramAPIError as e:
        return await _fail(m, bot, e)
    await _done(m, bot, f"✅ {mention(uid, await users_name(uid))} разбанен", "unban", uid)


@router_moderation_cmds.message(Command("kick"))
async def c_kick(m: Message, command: CommandObject, bot: Bot):
    t = await _target(m, command, bot)
    if not t:
        return
    uid, rest = t
    try:
        await moderation_kick(bot, uid)
    except TelegramAPIError as e:
        return await _fail(m, bot, e)
    reason = " ".join(rest)
    await _done(m, bot, f"👢 {mention(uid, await users_name(uid))} исключён из чата", "kick", uid, reason)


@router_moderation_cmds.message(Command("addadmin"))
async def c_addadmin(m: Message, command: CommandObject, bot: Bot):
    if not (m.from_user and roles_is_super(m.from_user.id)):
        return await safe_delete(bot, m.chat.id, m.message_id)
    r = await resolve(m, command, bot)
    await safe_delete(bot, m.chat.id, m.message_id)
    if not r:
        return await tmp(bot, "❌ Укажи пользователя: ответом, @username или ID.")
    uid = r[0]
    if roles_is_admin(uid):
        return await tmp(bot, "ℹ️ Он уже админ.")
    await roles_add(uid, m.from_user.id)
    await tmp(bot, f"👥 {mention(uid, await users_name(uid))} назначен админом.")
    await logs_log_event(bot, m.from_user.id, "admin_add", uid)


@router_moderation_cmds.message(Command("deladmin"))
async def c_deladmin(m: Message, command: CommandObject, bot: Bot):
    if not (m.from_user and roles_is_super(m.from_user.id)):
        return await safe_delete(bot, m.chat.id, m.message_id)
    r = await resolve(m, command, bot)
    await safe_delete(bot, m.chat.id, m.message_id)
    if not r:
        return await tmp(bot, "❌ Укажи пользователя: ответом, @username или ID.")
    uid = r[0]
    if roles_is_super(uid) or uid not in roles_admin_ids():
        return await tmp(bot, "ℹ️ Это не обычный админ.")
    await roles_remove(uid)
    await tmp(bot, f"👥 {mention(uid, await users_name(uid))} больше не админ.")
    await logs_log_event(bot, m.from_user.id, "admin_del", uid)


# ======== handlers/info.py ========
router_info = Router()
router_info.message.filter(F.chat.id == CHAT_ID)


@router_info.message(Command("rules"))
async def c_rules(m: Message, bot: Bot):
    await safe_delete(bot, m.chat.id, m.message_id)
    text = settings_get("rules_text") or "Правила не заданы."
    await tmp(bot, text, secs=60)


@router_info.message(Command("info"))
async def c_info(m: Message, command: CommandObject, bot: Bot):
    if not m.from_user:
        return
    await safe_delete(bot, m.chat.id, m.message_id)
    uid = m.from_user.id
    if roles_is_admin(uid):
        r = await resolve(m, command, bot)
        if r:
            uid = r[0]
    row = await users_get(uid)
    name = row["full_name"] if row else str(uid)
    role = "Главный админ" if roles_is_super(uid) else "Админ" if roles_is_admin(uid) else "Участник"
    cnt = await moderation_warn_count(uid)
    pun = {
        r["type"]: r["until"]
        for r in await db_fetch(
            "select type, until from punishments where user_id=$1 and (until is null or until>now())", uid
        )
    }

    def st(k):
        if k not in pun:
            return "нет"
        return "навсегда" if pun[k] is None else "до " + fmt_dt(pun[k])

    lines = [
        f"ℹ️ <b>{mention(uid, name)}</b>",
        f"🆔 <code>{uid}</code>",
    ]
    if row and row["username"]:
        lines.append(f"🔗 @{esc(row['username'])}")
    lines += [
        f"🎖 Роль: {role}",
        f"📅 Вход: {fmt_dt(row['joined_at']) if row and row['joined_at'].year > 2000 else '—'}",
        f"⚠️ Варны: {cnt}/{settings_get('warn_limit')}",
        f"🔇 Мут: {st('mute')}",
        f"🚫 Бан: {st('ban')}",
    ]
    await tmp(bot, "\n".join(lines), secs=45)


async def _list_cmd(m: Message, bot: Bot, kind: str):
    await safe_delete(bot, m.chat.id, m.message_id)
    if not (m.from_user and roles_is_admin(m.from_user.id)):
        return
    text, kb = await listing_render_list(kind, 0)
    msg = await safe_send(bot, CHAT_ID, text, reply_markup=kb, disable_notification=True)
    if msg:
        await scheduler_schedule_delete(CHAT_ID, msg.message_id, 120)


@router_info.message(Command("banlist"))
async def c_banlist(m: Message, bot: Bot):
    await _list_cmd(m, bot, "ban")


@router_info.message(Command("mutelist"))
async def c_mutelist(m: Message, bot: Bot):
    await _list_cmd(m, bot, "mute")


@router_info.callback_query(F.data.startswith("lst:"))
async def lst_cb(cb: CallbackQuery):
    if not roles_is_admin(cb.from_user.id):
        return await cb.answer("Только для админов.", show_alert=True)
    _, kind, page = cb.data.split(":")
    back = cb.message.chat.type == "private"
    text, kb = await listing_render_list(kind, int(page), back=back)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cb.answer()


# ======== handlers/protection.py ========
router_protection = Router()
router_protection.message.filter(F.chat.id == CHAT_ID)

URL_RE = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.I)


def links_in(m: Message) -> list[str]:
    text = m.text or m.caption or ""
    ents = m.entities or m.caption_entities or []
    urls = []
    for e in ents:
        if e.type == "url":
            urls.append(e.extract_from(text))
        elif e.type == "text_link" and e.url:
            urls.append(e.url)
    urls += URL_RE.findall(text)
    return urls


def _domain(u: str) -> str:
    u = u.strip()
    if "://" not in u:
        u = "http://" + u
    host = (urlparse(u).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _allowed(u: str, whitelist: list[str]) -> bool:
    d = _domain(u)
    return any(d == w or d.endswith("." + w) for w in whitelist)


async def check_filters(m: Message, uid: int):
    text = m.text or m.caption or ""
    if settings_get("filter_links_on"):
        urls = links_in(m)
        if urls:
            scope = settings_get("links_scope")
            applies = scope == "all" or await users_is_newbie(uid, settings_get("newbie_hours"))
            if applies:
                wl = settings_get("link_whitelist")
                if any(not _allowed(u, wl) for u in urls):
                    return "ссылка"
    if settings_get("filter_words_on") and text:
        low = text.lower()
        for w in settings_get("stop_words"):
            if w and w in low:
                return "стоп-слово"
    return None


async def violation(m: Message, bot: Bot, reason: str, action: str, dur: int):
    uid = m.from_user.id
    await safe_delete(bot, m.chat.id, m.message_id)
    res = None
    try:
        res = await moderation_apply_action(bot, uid, action, dur, f"авто: {reason}", bot.id)
    except TelegramAPIError:
        pass
    if action != "delete":
        note = f"🛡 {mention(uid, m.from_user.full_name)}: {reason}." + (f" {res}" if res else "")
        msg = await safe_send(bot, CHAT_ID, note, disable_notification=True)
        if msg:
            await scheduler_schedule_delete(CHAT_ID, msg.message_id, 15)
    await logs_log_event(bot, 0, f"auto_{reason}", uid, res or "сообщение удалено")


@router_protection.message()
async def guard(m: Message, bot: Bot):
    u = m.from_user
    if not u or u.is_bot or u.id == 777000 or m.is_automatic_forward:
        return
    await users_touch(u)
    if roles_is_admin(u.id):
        return
    reason = await check_filters(m, u.id)
    if reason:
        return await violation(m, bot, reason, settings_get("filter_action"), settings_get("filter_duration"))
    key = (m.text or m.caption or "").strip().lower() or str(m.content_type)
    if protection_check_spam(u.id, key):
        return await violation(m, bot, "спам", settings_get("antispam_action"), settings_get("antispam_duration"))
    if protection_check_flood(u.id):
        return await violation(m, bot, "флуд", "mute", settings_get("flood_duration"))


# ======== handlers/verify.py ========
log = logging.getLogger(__name__)
router_verify = Router()
router_verify.callback_query.filter(F.message.chat.type == "private")


async def _show(bot: Bot, chat_id: int, msg, text: str, markup=None):
    if msg:
        try:
            return await msg.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass
    return await safe_send(bot, chat_id, text, reply_markup=markup)


async def show_next(bot: Bot, uid: int, chat_id: int, msg=None) -> None:
    row = await db_fetchrow("select * from pending_captcha where user_id=$1", uid)
    if not row:
        await _show(bot, chat_id, msg, "ℹ️ Сейчас для тебя нет активной проверки. Зайди в чат — и она появится.")
        return
    if settings_get("sub_enabled") and settings_get("sub_channel") and not await captcha_is_subscribed(bot, uid):
        rows = []
        link = settings_get("sub_channel_link")
        if link:
            rows.append([btn("📢 Подписаться", url=link, style="primary")])
        rows.append([btn("✅ Проверить подписку", cb="sub:check", style="success")])
        title = esc(settings_get("sub_channel_title") or "канал")
        await _show(
            bot, chat_id, msg,
            f"📢 Сначала подпишись на канал «<b>{title}</b>», затем нажми «Проверить подписку».",
            kb(*rows),
        )
        return
    text, ans, opts = captcha_make_question()
    await db_execute("update pending_captcha set answer=$2, stage='captcha' where user_id=$1", uid, ans)
    left_min = max(0, int((row["expires_at"].astimezone(timezone.utc) - now()).total_seconds() // 60))
    markup = kb(
        [btn(str(o), cb=f"cap:{o}", style="primary") for o in opts[:2]],
        [btn(str(o), cb=f"cap:{o}", style="primary") for o in opts[2:]],
    )
    await _show(
        bot, chat_id, msg,
        f"🧮 Сколько будет <b>{text}</b>?\n\nПопыток: {row['attempts_left']} · осталось ~{left_min + 1} мин.",
        markup,
    )


@router_verify.callback_query(F.data == "sub:check")
async def sub_check(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    if not await db_fetchval("select 1 from pending_captcha where user_id=$1", uid):
        return await cb.answer("Проверка не активна.", show_alert=True)
    if not await captcha_is_subscribed(bot, uid):
        return await cb.answer("Подписка не найдена. Подпишись и нажми ещё раз.", show_alert=True)
    await cb.answer()
    await show_next(bot, uid, cb.message.chat.id, cb.message)


@router_verify.callback_query(F.data.startswith("cap:"))
async def on_answer(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    row = await db_fetchrow("select answer from pending_captcha where user_id=$1", uid)
    if not row:
        await cb.answer("Проверка не активна.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return
    if int(cb.data[4:]) == row["answer"]:
        await captcha_success(bot, uid)
        await cb.answer()
        await _show(bot, cb.message.chat.id, cb.message, "✅ Проверка пройдена! Можешь писать в чате.")
        return
    left = await db_fetchval(
        "update pending_captcha set attempts_left = attempts_left - 1 where user_id=$1 returning attempts_left", uid
    )
    if left is None:
        return await cb.answer()
    if left <= 0:
        await captcha_fail(bot, uid, "закончились попытки")
        await cb.answer()
        await _show(bot, cb.message.chat.id, cb.message,
                    "❌ Попытки закончились. Ты можешь зайти в чат снова и пройти проверку заново.")
        return
    await cb.answer(f"Неверно. Осталось попыток: {left}", show_alert=True)
    await show_next(bot, uid, cb.message.chat.id, cb.message)


# ======== handlers/private.py ========
router_private = Router()
router_private.message.filter(F.chat.type == "private")
router_private.callback_query.filter(F.message.chat.type == "private")

HELP = (
    "<b>Команды в чате</b>\n"
    "/warn, /unwarn, /warns — предупреждения\n"
    "/mute &lt;время&gt;, /unmute — мут (30m, 2h, 1d)\n"
    "/ban [время], /unban, /kick\n"
    "/banlist, /mutelist — списки\n"
    "/info — статус пользователя, /rules — правила\n"
    "Цель: ответом на сообщение, @username или ID.\n\n"
    "<b>Главные админы</b>: /admin — панель настроек, /addadmin, /deladmin."
)


def menu_kb(uid: int):
    rows = [[btn("🚫 Забаненные", cb="menu:ban", style="danger"),
             btn("🔇 Замученные", cb="menu:mute", style="primary")]]
    if roles_is_super(uid):
        rows.append([btn("⚙️ Панель настроек", cb="adm:m", style="success")])
    return kb(*rows)


@router_private.message(CommandStart())
async def start(m: Message, bot: Bot, state: FSMContext):
    u = m.from_user
    if not u:
        return
    if roles_is_admin(u.id):
        await state.clear()
        await m.answer("👋 Привет! Ты в панели управления чатом.", reply_markup=menu_kb(u.id))
        return
    await show_next(bot, u.id, m.chat.id)


@router_private.message(Command("help"))
async def help_cmd(m: Message):
    if m.from_user and roles_is_admin(m.from_user.id):
        await m.answer(HELP)


async def _list(m: Message, kind: str):
    if m.from_user and roles_is_admin(m.from_user.id):
        text, markup = await listing_render_list(kind, 0, back=True)
        await m.answer(text, reply_markup=markup)


@router_private.message(Command("banlist"))
async def banlist(m: Message):
    await _list(m, "ban")


@router_private.message(Command("mutelist"))
async def mutelist(m: Message):
    await _list(m, "mute")


@router_private.callback_query(F.data.startswith("menu:"))
async def menu_cb(cb: CallbackQuery):
    if not roles_is_admin(cb.from_user.id):
        return await cb.answer()
    act = cb.data.split(":")[1]
    if act == "home":
        text, markup = "👋 Панель управления чатом.", menu_kb(cb.from_user.id)
    else:
        text, markup = await listing_render_list(act, 0, back=True)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await cb.answer()


@router_private.callback_query(F.data.startswith("adm:"))
async def deny(cb: CallbackQuery):
    await cb.answer("Нет доступа.", show_alert=True)


@router_private.message(F.text)
async def admin_fallback(m: Message):
    # Обычным пользователям в ЛС бот не отвечает (кроме капчи/подписки)
    if m.from_user and roles_is_admin(m.from_user.id):
        await m.answer("Используй /start или /help.")


# ======== handlers/admin_panel.py ========
log = logging.getLogger(__name__)


class Input(StatesGroup):
    value = State()


router_admin_panel = Router()
router_admin_panel.message.filter(F.chat.type == "private", F.from_user.id.in_(SUPER_ADMINS))
router_admin_panel.callback_query.filter(F.message.chat.type == "private", F.from_user.id.in_(SUPER_ADMINS))


def I(t, k, label, **kw):
    return dict(t=t, k=k, l=label, **kw)


SECTIONS = {
    "greet": ("👋 Приветствие и правила",
              "Приветствие уходит в чат после проверки (удаляется через минуту). "
              "<code>{user}</code> заменится на упоминание. Пусто = стандартное.",
              [I("text", "welcome_text", "Приветствие"), I("text", "rules_text", "Правила")]),
    "sub": ("📢 Подписка на канал",
            "Бот должен быть админом канала. Пришли @username или ID канала.",
            [I("toggle", "sub_enabled", "Обязательная подписка"), I("channel", "sub_channel", "Канал")]),
    "captcha": ("🧮 Капча", "Отсчёт времени идёт с момента входа в чат.",
                [I("dur", "captcha_time", "Время на капчу", min=30),
                 I("int", "captcha_attempts", "Попытки", min=1, max=10)]),
    "warns": ("⚠️ Варны и муты", "При лимите варнов применяется наказание.",
              [I("int", "warn_limit", "Лимит варнов", min=1, max=20),
               I("choice", "warn_action", "Наказание", o=["mute", "ban"]),
               I("dur0", "warn_duration", "Срок наказания (0 = навсегда)"),
               I("toggle", "warn_reset", "Обнулять варны после наказания"),
               I("dur", "default_mute", "Мут по умолчанию", min=60),
               I("int", "cmd_cleanup", "Удалять ответы бота, сек", min=5, max=600)]),
    "spam": ("🔁 Антиспам", "Одинаковые сообщения за окно времени.",
             [I("toggle", "antispam_on", "Антиспам"),
              I("int", "antispam_repeats", "Повторов для срабатывания", min=2, max=20),
              I("int", "antispam_window", "Окно, сек", min=5, max=600),
              I("choice", "antispam_action", "Действие", o=["delete", "warn", "mute", "ban"]),
              I("dur0", "antispam_duration", "Срок мута/бана")]),
    "flood": ("⚡ Антифлуд", "N сообщений за M секунд = мут.",
              [I("toggle", "antiflood_on", "Антифлуд"),
               I("int", "flood_msgs", "Сообщений", min=3, max=50),
               I("int", "flood_secs", "За секунд", min=1, max=60),
               I("dur", "flood_duration", "Срок мута", min=60)]),
    "raid": ("🛡 Антирейд", "Много входов за минуту включают усиленную проверку.",
             [I("toggle", "antiraid_on", "Антирейд"),
              I("int", "raid_threshold", "Порог входов в минуту", min=3, max=100),
              I("dur", "raid_duration", "Длительность режима", min=60),
              I("dur", "raid_captcha_time", "Время на капчу в рейд", min=30),
              I("int", "raid_attempts", "Попыток в рейд", min=1, max=5),
              I("toggle", "raid_block", "Блокировать вход (кик)")]),
    "filters": ("🧹 Фильтры", "Админы фильтрами не затрагиваются.",
                [I("toggle", "filter_links_on", "Фильтр ссылок"),
                 I("choice", "links_scope", "Кого проверять", o=["newbies", "all"]),
                 I("int", "newbie_hours", "Новичок, часов", min=1, max=720),
                 I("list", "link_whitelist", "Белый список доменов"),
                 I("toggle", "filter_words_on", "Фильтр стоп-слов"),
                 I("list", "stop_words", "Стоп-слова"),
                 I("choice", "filter_action", "Действие", o=["delete", "warn", "mute"]),
                 I("dur", "filter_duration", "Срок мута", min=60)]),
    "logs": ("📜 Логи", "", [I("toggle", "notify_captcha", "Уведомлять о капче")]),
}
EXTRA = {"admins": "👥 Админы"}
FIELDS = {it["k"]: (sec, it) for sec, (_, _, items) in SECTIONS.items() for it in items}
CH = {"delete": "только удалить", "warn": "варн", "mute": "мут", "ban": "бан",
      "newbies": "новички", "all": "все"}
PROMPTS = {
    "int": "Пришли число ({min}–{max}).",
    "dur": "Пришли длительность: 30s, 10m, 2h, 1d (число без буквы = секунды).",
    "dur0": "Пришли длительность: 30m, 2h, 1d. 0 = навсегда.",
    "text": "Пришли новый текст (форматирование сохранится).",
    "channel": "Пришли @username или ID канала. Бот должен быть админом канала.",
}


def fmt_value(it) -> str:
    v = settings_get(it["k"])
    t = it["t"]
    if t == "int":
        return str(v)
    if t in ("dur", "dur0"):
        return fmt_duration(v)
    if t == "choice":
        return CH.get(v, v)
    if t == "text":
        return "задан" if v else "не задан"
    if t == "channel":
        return settings_get("sub_channel_title") or "не задан"
    return ""


def main_view():
    keys = [k for k in SECTIONS if k != "logs"] + ["admins", "logs"]
    titles = {k: SECTIONS[k][0] for k in SECTIONS} | EXTRA
    rows, row = [], []
    for k in keys:
        row.append(btn(titles[k], cb=f"adm:s:{k}", style="primary"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return "⚙️ <b>Панель главных админов</b>\nВыбери раздел.", kb(*rows)


def section_view(sec: str):
    title, hint, items = SECTIONS[sec]
    rows = []
    for it in items:
        t, k, v = it["t"], it["k"], settings_get(it["k"])
        if t == "toggle":
            rows.append([btn(f"{it['l']}: {'вкл' if v else 'выкл'}", cb=f"adm:t:{k}",
                             style="success" if v else "danger")])
        elif t == "choice":
            rows.append([btn(f"{it['l']}: {fmt_value(it)}", cb=f"adm:c:{k}", style="primary")])
        elif t == "list":
            rows.append([btn(f"📋 {it['l']} ({len(v)})", cb=f"adm:l:{k}", style="primary")])
        elif t in ("text", "channel"):
            rows.append([btn(f"✏️ {it['l']}: {fmt_value(it)}", cb=f"adm:i:{k}", style="primary"),
                         btn("🗑", cb=f"adm:x:{k}", style="danger")])
        else:
            rows.append([btn(f"{it['l']}: {fmt_value(it)}", cb=f"adm:i:{k}", style="primary")])
    rows.append([btn("⬅️ Назад", cb="adm:m", style="primary")])
    return f"<b>{title}</b>\n\n{hint}", kb(*rows)


def list_view(k: str):
    sec, it = FIELDS[k]
    items = settings_get(k)
    body = "\n".join(f"{i + 1}. {esc(x)}" for i, x in enumerate(items[:40])) or "Пусто."
    rows = [[btn(f"❌ {x[:30]}", cb=f"adm:ld:{k}:{i}", style="danger")] for i, x in enumerate(items[:20])]
    rows.append([btn("➕ Добавить", cb=f"adm:la:{k}", style="success"),
                 btn("🗑 Очистить", cb=f"adm:lc:{k}", style="danger")])
    rows.append([btn("⬅️ Назад", cb=f"adm:s:{sec}", style="primary")])
    return f"<b>{it['l']}</b>\n\n{body}", kb(*rows)


async def admins_view():
    ids = sorted(roles_admin_ids())
    lines = ["<b>👥 Админы</b>", "", "Главные: " + ", ".join(f"<code>{i}</code>" for i in sorted(SUPER_ADMINS)), ""]
    rows = []
    for uid in ids:
        n = await users_name(uid)
        lines.append(f"• {esc(n)} (<code>{uid}</code>)")
        rows.append([btn(f"❌ {n[:25]} · {uid}", cb=f"adm:ar:{uid}", style="danger")])
    if not ids:
        lines.append("Обычных админов пока нет.")
    rows.append([btn("➕ Добавить админа", cb="adm:aa", style="success")])
    rows.append([btn("⬅️ Назад", cb="adm:m", style="primary")])
    return "\n".join(lines), kb(*rows)


async def logs_view():
    rows_db = await logs_recent(15)
    st = await logs_stats()
    lines = ["<b>📜 Логи</b> (последние 15)", ""]
    for r in rows_db:
        lines.append(
            f"<code>{r['ts']:%d.%m %H:%M}</code> {esc(r['action'])} "
            f"{r['actor'] or '🤖'}→{r['target'] or '—'} {esc((r['details'] or '')[:50])}"
        )
    if not rows_db:
        lines.append("Записей нет.")
    if st:
        lines += ["", "<b>За 24 часа:</b> " + ", ".join(f"{esc(s['action'])}: {s['c']}" for s in st)]
    on = settings_get("notify_captcha")
    markup = kb(
        [btn(f"Уведомлять о капче: {'вкл' if on else 'выкл'}", cb="adm:t:notify_captcha",
             style="success" if on else "danger")],
        [btn("🔄 Обновить", cb="adm:s:logs", style="success")],
        [btn("⬅️ Назад", cb="adm:m", style="primary")],
    )
    return "\n".join(lines)[:4000], markup


async def view(sec: str):
    if sec == "admins":
        return await admins_view()
    if sec == "logs":
        return await logs_view()
    return section_view(sec)


async def edit(cb: CallbackQuery, text: str, markup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await cb.answer()


@router_admin_panel.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    text, markup = main_view()
    await m.answer(text, reply_markup=markup)


@router_admin_panel.callback_query(F.data == "adm:m")
async def to_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, *main_view())


@router_admin_panel.callback_query(F.data.startswith("adm:s:"))
async def to_section(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, *await view(cb.data[6:]))


@router_admin_panel.callback_query(F.data.startswith("adm:t:"))
async def toggle(cb: CallbackQuery, bot: Bot):
    k = cb.data[6:]
    if k not in FIELDS:
        return await cb.answer()
    new = not settings_get(k)
    await settings_put(k, new)
    await logs_log_event(bot, cb.from_user.id, "setting", None, f"{k} = {new}")
    await edit(cb, *await view(FIELDS[k][0]))


@router_admin_panel.callback_query(F.data.startswith("adm:c:"))
async def cycle(cb: CallbackQuery, bot: Bot):
    k = cb.data[6:]
    if k not in FIELDS:
        return await cb.answer()
    opts = FIELDS[k][1]["o"]
    cur = settings_get(k)
    new = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
    await settings_put(k, new)
    await logs_log_event(bot, cb.from_user.id, "setting", None, f"{k} = {new}")
    await edit(cb, *await view(FIELDS[k][0]))


@router_admin_panel.callback_query(F.data.startswith("adm:x:"))
async def clear_value(cb: CallbackQuery, bot: Bot):
    k = cb.data[6:]
    if k not in FIELDS:
        return await cb.answer()
    if k == "sub_channel":
        for key, val in (("sub_channel", 0), ("sub_channel_title", ""), ("sub_channel_link", ""), ("sub_enabled", False)):
            await settings_put(key, val)
    else:
        await settings_put(k, "")
    await logs_log_event(bot, cb.from_user.id, "setting", None, f"{k} очищено")
    await edit(cb, *await view(FIELDS[k][0]))


@router_admin_panel.callback_query(F.data.startswith("adm:i:"))
async def ask_value(cb: CallbackQuery, state: FSMContext):
    k = cb.data[6:]
    if k not in FIELDS:
        return await cb.answer()
    sec, it = FIELDS[k]
    await state.set_state(Input.value)
    await state.update_data(mode="set", key=k, sec=sec)
    prompt = PROMPTS[it["t"]].format(min=it.get("min", 1), max=it.get("max", 0))
    if k == "welcome_text":
        prompt += " Для упоминания используй {user}."
    await edit(cb, f"✏️ <b>{it['l']}</b>\n\n{prompt}",
               kb([btn("✖️ Отмена", cb=f"adm:s:{sec}", style="danger")]))


@router_admin_panel.callback_query(F.data.startswith("adm:l:"))
async def show_list(cb: CallbackQuery):
    k = cb.data[6:]
    if k in FIELDS:
        await edit(cb, *list_view(k))


@router_admin_panel.callback_query(F.data.startswith("adm:la:"))
async def list_add(cb: CallbackQuery, state: FSMContext):
    k = cb.data[7:]
    if k not in FIELDS:
        return await cb.answer()
    await state.set_state(Input.value)
    await state.update_data(mode="ladd", key=k, sec=FIELDS[k][0])
    hint = "домены (например example.com)" if k == "link_whitelist" else "слова или фразы"
    await edit(cb, f"➕ Пришли {hint}: по одному в строке или через запятую.",
               kb([btn("✖️ Отмена", cb=f"adm:l:{k}", style="danger")]))


@router_admin_panel.callback_query(F.data.startswith("adm:ld:"))
async def list_del(cb: CallbackQuery, bot: Bot):
    _, _, k, idx = cb.data.split(":")
    if k in FIELDS:
        items = settings_get(k)
        if int(idx) < len(items):
            removed = items.pop(int(idx))
            await settings_put(k, items)
            await logs_log_event(bot, cb.from_user.id, "setting", None, f"{k}: удалено «{removed}»")
        await edit(cb, *list_view(k))


@router_admin_panel.callback_query(F.data.startswith("adm:lc:"))
async def list_clear(cb: CallbackQuery, bot: Bot):
    k = cb.data[7:]
    if k in FIELDS:
        await settings_put(k, [])
        await logs_log_event(bot, cb.from_user.id, "setting", None, f"{k} очищен")
        await edit(cb, *list_view(k))


@router_admin_panel.callback_query(F.data == "adm:aa")
async def admin_add_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Input.value)
    await state.update_data(mode="aadd", key="", sec="admins")
    await edit(cb, "👥 Пришли ID, @username или перешли сообщение от человека.",
               kb([btn("✖️ Отмена", cb="adm:s:admins", style="danger")]))


@router_admin_panel.callback_query(F.data.startswith("adm:ar:"))
async def admin_remove(cb: CallbackQuery, bot: Bot):
    uid = int(cb.data[7:])
    if uid in roles_admin_ids():
        await roles_remove(uid)
        await logs_log_event(bot, cb.from_user.id, "admin_del", uid)
    await edit(cb, *await admins_view())


async def _set_channel(bot: Bot, raw: str) -> None:
    raw = raw.replace("https://", "").replace("http://", "").replace("t.me/", "@").strip()
    if raw.lstrip("-").isdigit():
        ref = int(raw)
    else:
        ref = raw if raw.startswith("@") else "@" + raw
    try:
        chat = await bot.get_chat(ref)
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramAPIError:
        raise ValueError("Канал не найден или бот в него не добавлен.")
    if me.status not in ("administrator", "creator"):
        raise ValueError("Бот должен быть администратором канала.")
    link = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
    if not link:
        try:
            link = await bot.export_chat_invite_link(chat.id)
        except TelegramAPIError:
            link = ""
    await settings_put("sub_channel", chat.id)
    await settings_put("sub_channel_title", chat.title or "")
    await settings_put("sub_channel_link", link or "")


async def _apply_set(m: Message, bot: Bot, k: str, raw: str) -> None:
    it = FIELDS[k][1]
    t = it["t"]
    if t == "int":
        if not raw.isdigit() or not (it["min"] <= int(raw) <= it["max"]):
            raise ValueError(f"Нужно число от {it['min']} до {it['max']}.")
        val = int(raw)
    elif t in ("dur", "dur0"):
        d = parse_duration(raw)
        if d is None or (d == 0 and t == "dur") or (0 < d < it.get("min", 1)):
            raise ValueError("Неверный формат или слишком малое значение. Пример: 10m, 2h, 1d.")
        val = d
    elif t == "text":
        val = m.html_text
        if len(val) > 3000:
            raise ValueError("Слишком длинный текст (максимум 3000 символов).")
    elif t == "channel":
        await _set_channel(bot, raw)
        await logs_log_event(bot, m.from_user.id, "setting", None, f"канал подписки: {settings_get('sub_channel_title')}")
        return
    else:
        raise ValueError("Это поле так не меняется.")
    await settings_put(k, val)
    shown = "текст обновлён" if t == "text" else val
    await logs_log_event(bot, m.from_user.id, "setting", None, f"{k} = {shown}")


async def _apply_ladd(m: Message, bot: Bot, k: str, raw: str) -> None:
    parts = [p.strip().lower() for p in raw.replace(",", "\n").splitlines() if p.strip()]
    if not parts:
        raise ValueError("Пусто.")
    items = settings_get(k)
    for p in parts:
        if k == "link_whitelist":
            p = p.replace("https://", "").replace("http://", "").split("/")[0]
            p = p[4:] if p.startswith("www.") else p
        p = p[:50]
        if p and p not in items:
            items.append(p)
    await settings_put(k, items)
    await logs_log_event(bot, m.from_user.id, "setting", None, f"{k}: +{len(parts)}")


async def _apply_aadd(m: Message, bot: Bot, raw: str) -> None:
    uid = None
    fo = m.forward_origin
    if fo is not None and getattr(fo, "type", "") == "user":
        uid = fo.sender_user.id
    elif raw.lstrip("-").isdigit():
        uid = int(raw)
    elif raw.startswith("@"):
        uid = await users_find_by_username(raw)
        if not uid:
            raise ValueError("Не знаю такого @username. Пришли его ID или перешли его сообщение.")
    if not uid:
        raise ValueError("Не удалось определить пользователя.")
    if roles_is_admin(uid):
        raise ValueError("Он уже админ.")
    await roles_add(uid, m.from_user.id)
    await logs_log_event(bot, m.from_user.id, "admin_add", uid)
    await safe_send(bot, uid, "👥 Тебя назначили администратором чата. Нажми /start, чтобы открыть меню.")


@router_admin_panel.message(Input.value, F.text, ~F.text.startswith("/"))
async def got_value(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    mode, k, sec = data.get("mode"), data.get("key"), data.get("sec")
    raw = m.text.strip()
    try:
        if mode == "set":
            await _apply_set(m, bot, k, raw)
        elif mode == "ladd":
            await _apply_ladd(m, bot, k, raw)
        elif mode == "aadd":
            await _apply_aadd(m, bot, raw)
    except ValueError as e:
        return await m.answer(f"❌ {esc(str(e))}\nПопробуй ещё раз или нажми «Отмена».")
    await state.clear()
    if mode == "ladd":
        text, markup = list_view(k)
    else:
        text, markup = await view(sec)
    await m.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)


# ======== main.py ========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


async def health(_request):
    return web.Response(text="ok")


async def start_web() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("health-сервер на порту %s", PORT)
    return runner


async def main() -> None:
    await db_init(DATABASE_URL)
    await settings_load()
    await roles_load()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    @dp.error()
    async def on_error(event: ErrorEvent):
        log.exception("Ошибка при обработке апдейта", exc_info=event.exception)
        return True

    dp.include_routers(
        router_guard, router_chat_events, router_moderation_cmds, router_info,
        router_admin_panel, router_verify, router_private, router_protection,
    )

    runner = await start_web()
    await bot.delete_webhook(drop_pending_updates=False)
    task = asyncio.create_task(scheduler_run(bot))
    me = await bot.me()
    log.info("Бот @%s запущен, чат %s", me.username, CHAT_ID)
    try:
        await dp.start_polling(
            bot, allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"]
        )
    finally:
        task.cancel()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
