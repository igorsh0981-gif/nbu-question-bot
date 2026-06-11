# ═══════════════════════════════════════════════════════════════
# NBU Question Bot — тихий режим
# Бот читает чат молча, пишет в таблицу, уведомляет PM в личку
# ═══════════════════════════════════════════════════════════════

import asyncio
import logging
import json
import re
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

import gspread

import anthropic

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Конфигурация ────────────────────────────────────────────────
import os

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
ANTHROPIC_KEY  = os.environ['ANTHROPIC_API_KEY']
SHEET_ID       = os.environ['SHEET_ID']
SHEET_NAME     = 'Questions'
GROUP_CHAT_ID  = int(os.environ.get('GROUP_CHAT_ID', '-1003963999739'))
PM_CHAT_ID     = int(os.environ.get('PM_CHAT_ID', '5281759957'))
GOOGLE_TOKEN_JSON = os.environ['GOOGLE_TOKEN_JSON']

# ── Инициализация ───────────────────────────────────────────────

bot       = Bot(token=TELEGRAM_TOKEN)
dp        = Dispatcher()
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

def make_gspread():
    d = json.loads(os.environ['GOOGLE_TOKEN_JSON'])
    c = Credentials(token=d['token'], refresh_token=d['refresh_token'],
                    token_uri=d['token_uri'], client_id=d['client_id'],
                    client_secret=d['client_secret'], scopes=d['scopes'])
    if c.expired: c.refresh(Request())
    return gspread.authorize(c)

gc = make_gspread()
sh = gc.open_by_key(SHEET_ID)
ws = sh.worksheet(SHEET_NAME)

# ── Google Sheets хелперы ────────────────────────────────────────

def get_all_rows() -> list[dict]:
    return ws.get_all_records()

def append_row(data: dict):
    headers = ws.row_values(1)
    row = [str(data.get(h, '')) for h in headers]
    ws.append_row(row, value_input_option='USER_ENTERED')

def update_row_by_id(question_id, updates: dict) -> bool:
    try:
        records = ws.get_all_records()
        headers = ws.row_values(1)
        for i, r in enumerate(records):
            if str(r.get('id', '')) == str(question_id):
                row_num = i + 2  # +1 header +1 index
                for col_name, value in updates.items():
                    if col_name in headers:
                        col_idx = headers.index(col_name) + 1
                        ws.update_cell(row_num, col_idx, str(value))
                return True
        return False
    except Exception as e:
        log.error(f"update_row_by_id error: {e}")
        return False

def find_row_by_id(question_id) -> Optional[dict]:
    rows = get_all_rows()
    return next((r for r in rows if str(r.get('id', '')) == str(question_id)), None)

def today_str() -> str:
    now = datetime.now()
    return f"{now.day:02d}.{now.month:02d}.{str(now.year)[2:]}"

def crit_emoji(c: str) -> str:
    return {'RED': '🔴', 'YELLOW': '🟡', 'GREEN': '🟢'}.get(c, '⚪')

def check_duplicate(question_text: str) -> Optional[dict]:
    rows = get_all_rows()
    short_new = question_text.lower()[:25]
    for r in rows:
        if r.get('status') != 'ОТКРЫТА':
            continue
        short_exist = (r.get('question', '')).lower()[:25]
        if len(short_exist) > 10 and short_exist == short_new[:len(short_exist)]:
            return r
    return None

# ── Claude функции ───────────────────────────────────────────────

def ai_analyze_message(text: str, asked_by: str, reply_to: str = '') -> dict:
    """
    Claude решает:
    1. Является ли сообщение вопросом
    2. Если да — классифицирует его
    """
    prompt = f"""Ты анализатор сообщений из Telegram-чата банковского проекта НБУ Узбекистана (мобильный банк Milliy 3.0).

Сообщение: {text}
Автор: {asked_by}
{f'Reply на сообщение от: {reply_to}' if reply_to else ''}

Определи: является ли это сообщение вопросом, требующим ответа и отслеживания?

Вопросом считается:
- Прямой вопрос к команде или конкретному человеку
- Запрос информации, статуса, документа, доступа
- Проблема требующая решения
- Уточнение по задаче

НЕ является вопросом:
- Обычные реплики, приветствия, благодарности
- Утверждения и информирование
- Ответы на чужие вопросы

Верни ТОЛЬКО JSON без markdown:
{{
  "is_question": true/false,
  "responsible": "@username если явно указан адресат иначе null",
  "block": "Авторизация|Платежи|Интеграция|Документы|Доступы|Тестирование|Дизайн|Другое",
  "criticality": "RED|YELLOW|GREEN",
  "impact": "одна фраза до 80 символов — на что влияет",
  "release": "номер релиза если упомянут иначе пустая строка",
  "question_clean": "очищенный текст вопроса без лишних слов"
}}"""

    try:
        resp = ai_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = resp.content[0].text.strip()
        # Убираем markdown если вдруг появился
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'```$', '', raw).strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"Claude analyze error: {e}")
        return {'is_question': False}

def ai_verify_answer(question_text: str, answer_text: str) -> dict:
    """Проверяет является ли сообщение ответом на вопрос."""
    prompt = f"""Является ли сообщение ответом на вопрос?

Вопрос: {question_text}
Сообщение: {answer_text}

Верни ТОЛЬКО JSON:
{{"isAnswer": true/false, "confidence": 0.0-1.0, "summary": "краткое резюме ответа одним предложением"}}"""

    try:
        resp = ai_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=150,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'```$', '', raw).strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"Claude verify error: {e}")
        return {'isAnswer': False, 'confidence': 0, 'summary': ''}

# ── Уведомление PM ───────────────────────────────────────────────

async def notify_pm(text: str):
    """Отправляет уведомление в личку PM."""
    try:
        await bot.send_message(PM_CHAT_ID, text)
    except Exception as e:
        log.error(f"PM notify error: {e}")

# ── Основной хендлер ─────────────────────────────────────────────

@dp.message(F.text)
async def handle_message(msg: Message):
    text = msg.text.strip()
    chat_id = str(msg.chat.id)
    from_user = msg.from_user
    username = f'@{from_user.username}' if from_user.username else None
    asked_by = ' '.join(filter(None, [username, from_user.first_name])).strip()

    # ── Команды PM (только для PM_CHAT_ID или из группы) ────────
    if re.match(r'^/(close|answer|status|resp|report)\s*', text):
        await handle_command(msg, text, chat_id, asked_by)
        return

    # ── Все остальные сообщения — анализируем через Claude ───────
    # Reply — сначала проверяем является ли это ответом по msg_id
    if msg.reply_to_message:
        reply_msg_id = str(msg.reply_to_message.message_id)
        rows = get_all_rows()
        matched = next(
            (r for r in rows
             if str(r.get('msg_id', '')) == reply_msg_id
             and r.get('status') == 'ОТКРЫТА'),
            None
        )
        if matched:
            verdict = ai_verify_answer(matched.get('question', ''), text)
            if verdict.get('isAnswer') and verdict.get('confidence', 0) >= 0.6:
                update_row_by_id(matched['id'], {
                    'answer': text,
                    'status': 'ЗАКРЫТА',
                    'resolved_date': today_str()
                })
                log.info(f"Closed #{matched['id']} via reply")
                await notify_pm(
                    f'✅ Вопрос #{matched["id"]} закрыт через reply\n'
                    f'💬 {verdict.get("summary") or text[:80]}\n'
                    f'Ответил: {asked_by}'
                )
                return
            # Reply не прошёл порог — если не явный вопрос, игнорируем
            if not text.endswith('?') and not re.search(r'\bвопрос\b', text, re.I):
                return

    # Reply — кому адресован (для нового вопроса)
    reply_to_user = ''
    reply_to_name = ''
    if msg.reply_to_message and msg.reply_to_message.from_user:
        rf = msg.reply_to_message.from_user
        reply_to_user = f'@{rf.username}' if rf.username else ''
        reply_to_name = ' '.join(filter(None, [rf.first_name, rf.last_name])).strip()

    reply_display = reply_to_user or reply_to_name

    # Claude анализирует
    ai = ai_analyze_message(text, asked_by, reply_display)

    if ai.get('is_question'):
        await handle_new_question(msg, text, ai, chat_id, asked_by, reply_to_user, reply_to_name)
    else:
        await handle_possible_answer(msg, text, chat_id, asked_by)


async def handle_new_question(msg, text, ai, chat_id, asked_by, reply_to_user, reply_to_name):
    """Тихо фиксирует вопрос в таблице, уведомляет PM."""

    question_text = ai.get('question_clean') or text

    # Дубль?
    duplicate = check_duplicate(question_text)
    if duplicate:
        await notify_pm(
            f'♻️ Дубль вопроса в чате\n'
            f'Оригинал: #{duplicate["id"]}\n'
            f'Новое сообщение: {question_text[:80]}\n'
            f'Автор: {asked_by}'
        )
        return

    # Определяем ответственного
    responsible = ''
    responsible_name = ''
    ai_resp = ai.get('responsible', '')
    if ai_resp and ai_resp.startswith('@'):
        responsible = ai_resp
        responsible_name = ai_resp
    elif reply_to_user:
        responsible = reply_to_user
        responsible_name = reply_to_name or reply_to_user
    elif reply_to_name:
        responsible = ''
        responsible_name = reply_to_name
    else:
        # Mention в тексте
        m = re.search(r'@(\w+)', text)
        if m:
            responsible = f'@{m.group(1)}'
            responsible_name = responsible

    question_id = int(datetime.now().timestamp() * 1000)
    crit = ai.get('criticality', 'YELLOW')

    row = {
        'id':               question_id,
        'release':          ai.get('release', ''),
        'block':            ai.get('block', 'Другое'),
        'question':         question_text,
        'answer':           '',
        'criticality':      crit,
        'impact':           ai.get('impact', ''),
        'created_date':     today_str(),
        'resolved_date':    '',
        'status':           'ОТКРЫТА',
        'source':           'bot',
        'responsible':      responsible,
        'responsible_name': responsible_name,
        'asked_by':         asked_by,
        'chat_id':          chat_id,
        'chat_name':        msg.chat.title or msg.chat.username or 'Private',
        'msg_id':           str(msg.message_id),
    }

    append_row(row)
    log.info(f"New question #{question_id}: {question_text[:50]}")

    # Уведомление PM в личку (не в чат!)
    resp_display = responsible or responsible_name or '⚠️ не определён'
    await notify_pm(
        f'📋 Новый вопрос зафиксирован\n'
        f'🆔 #{question_id}\n'
        f'{crit_emoji(crit)} {crit} | 📦 {ai.get("block","Другое")}\n'
        f'👤 {resp_display}\n'
        f'💬 {question_text[:100]}\n'
        f'👤 Автор: {asked_by}\n\n'
        f'/resp {question_id} @username — назначить\n'
        f'/close {question_id} <ответ> — закрыть'
    )


async def handle_possible_answer(msg, text, chat_id, asked_by):
    """Проверяет является ли сообщение ответом на открытый вопрос."""
    rows = get_all_rows()

    # Явный ID в тексте
    id_match = re.search(r'\b(\d{13,})\b', text)
    matched = None
    if id_match:
        matched = next((r for r in rows if str(r.get('id', '')) == id_match.group(1)), None)

    # Единственный открытый вопрос в чате
    if not matched:
        open_in_chat = [
            r for r in rows
            if r.get('status') == 'ОТКРЫТА' and str(r.get('chat_id', '')) == chat_id
        ]
        if len(open_in_chat) == 1:
            matched = open_in_chat[0]

    if not matched:
        return

    # Claude верифицирует
    verdict = ai_verify_answer(matched.get('question', ''), text)

    if verdict.get('isAnswer') and verdict.get('confidence', 0) >= 0.75:
        update_row_by_id(matched['id'], {
            'answer':        text,
            'status':        'ЗАКРЫТА',
            'resolved_date': today_str()
        })
        log.info(f"Auto-closed #{matched['id']}")

        # Уведомление PM — молча закрыли
        await notify_pm(
            f'✅ Вопрос #{matched["id"]} автоматически закрыт\n'
            f'💬 {verdict.get("summary") or text[:80]}\n'
            f'Ответил: {asked_by}'
        )


# ── Команды (для PM) ─────────────────────────────────────────────

async def handle_command(msg: Message, text: str, chat_id: str, asked_by: str):

    # /close ID текст
    m = re.match(r'^/close\s+(\d+)(?:\s+([\s\S]+))?$', text)
    if m:
        qid, answer = m.group(1), m.group(2) or 'Закрыт'
        ok = update_row_by_id(int(qid), {
            'status': 'ЗАКРЫТА', 'answer': answer, 'resolved_date': today_str()
        })
        await msg.reply(f'✅ #{qid} закрыт' if ok else f'❌ #{qid} не найден')
        return

    # /answer ID текст
    m = re.match(r'^/answer\s+(\d+)(?:\s+([\s\S]+))?$', text)
    if m:
        qid, answer = m.group(1), m.group(2) or ''
        ok = update_row_by_id(int(qid), {'answer': answer})
        await msg.reply(f'💬 Ответ на #{qid} сохранён' if ok else f'❌ #{qid} не найден')
        return

    # /status ID
    m = re.match(r'^/status\s+(\d+)', text)
    if m:
        row = find_row_by_id(m.group(1))
        if not row:
            await msg.reply(f'❌ #{m.group(1)} не найден')
            return
        crit = row.get('criticality', '')
        s = '✅' if row.get('status') == 'ЗАКРЫТА' else '⏳'
        lines = [
            f'{s} #{row["id"]}',
            f'📦 {row.get("block","—")}  {crit_emoji(crit)} {crit}',
            f'👤 {row.get("responsible") or row.get("responsible_name") or "не назначен"}',
            f'📅 {row.get("created_date","—")}',
            f'✅ Закрыт: {row["resolved_date"]}' if row.get('resolved_date') else '',
            f'🔄 {row.get("status","—")}',
            f'📎 {row["impact"]}' if row.get('impact') else '',
            f'💬 {row["answer"]}' if row.get('answer') else '',
        ]
        await msg.reply('\n'.join(l for l in lines if l))
        return

    # /resp ID @username
    m = re.match(r'^/resp\s+(\d+)\s+(.+)', text)
    if m:
        qid, val = m.group(1), m.group(2).strip()
        responsible = val if val.startswith('@') else ''
        ok = update_row_by_id(int(qid), {
            'responsible': responsible,
            'responsible_name': val
        })
        warn = '\n⚠️ Нет @username — тег в напоминаниях недоступен' if not responsible else ''
        await msg.reply(f'👤 #{qid}: {val}{warn}' if ok else f'❌ #{qid} не найден')
        return

    # /report
    if text.strip() == '/report':
        rows = get_all_rows()
        open_rows = [r for r in rows if r.get('status') == 'ОТКРЫТА']
        if not open_rows:
            await msg.reply('✅ Открытых вопросов нет!')
            return
        red = [r for r in open_rows if r.get('criticality') == 'RED']
        yel = [r for r in open_rows if r.get('criticality') == 'YELLOW']
        grn = [r for r in open_rows if r.get('criticality') == 'GREEN']
        sorted_rows = red + yel + grn + [r for r in open_rows if not r.get('criticality')]
        lines = [f'📊 Открытые: {len(open_rows)}\n🔴 {len(red)}  🟡 {len(yel)}  🟢 {len(grn)}\n']
        for i, r in enumerate(sorted_rows[:20]):
            resp = r.get('responsible') or r.get('responsible_name') or '❓'
            lines.append(
                f'{i+1}. {crit_emoji(r.get("criticality",""))} #{r["id"]} '
                f'{r.get("question","")[:45]}\n   → {resp}'
            )
        await msg.reply('\n'.join(lines))
        return


# ── Планировщик ──────────────────────────────────────────────────

async def daily_reminder():
    """Будни 10:00 — напоминания ответственным в чат."""
    log.info("Daily reminder triggered")
    rows = get_all_rows()
    open_rows = [r for r in rows if r.get('status') == 'ОТКРЫТА']
    if not open_rows:
        return

    by_chat = {}
    for r in open_rows:
        cid = str(r.get('chat_id', ''))
        if not cid:
            continue
        if cid not in by_chat:
            by_chat[cid] = []
        by_chat[cid].append(r)

    for chat_id, chat_rows in by_chat.items():
        sorted_rows = (
            [r for r in chat_rows if r.get('criticality') == 'RED'] +
            [r for r in chat_rows if r.get('criticality') == 'YELLOW'] +
            [r for r in chat_rows if r.get('criticality') == 'GREEN'] +
            [r for r in chat_rows if not r.get('criticality')]
        )

        # Теги ответственных
        tags = list(set(
            r['responsible'] for r in sorted_rows
            if r.get('responsible', '').startswith('@')
        ))
        no_tag = list(set(
            r.get('responsible_name', '') for r in sorted_rows
            if r.get('responsible_name') and not r.get('responsible', '').startswith('@')
        ))

        tag_line = ' '.join(tags)
        if no_tag:
            tag_line += (' ' if tag_line else '') + ', '.join(filter(None, no_tag)) + ' (нет @)'
        tag_line = f'{tag_line} — ожидаем ответов:' if tag_line else '⚠️ Открытые вопросы:'

        red_count = sum(1 for r in sorted_rows if r.get('criticality') == 'RED')
        lines = [f'🔔 Открытые вопросы\n{tag_line}\n']
        for i, r in enumerate(sorted_rows[:15]):
            resp = r.get('responsible') or r.get('responsible_name') or '❓ не назначен'
            lines.append(
                f'{i+1}. {crit_emoji(r.get("criticality",""))} '
                f'#{r["id"]} {r.get("question","")[:45]}\n   → {resp}'
            )
        lines.append(f'\n🔴 Критичных: {red_count} из {len(sorted_rows)}')

        try:
            await bot.send_message(int(chat_id), '\n'.join(lines))
        except Exception as e:
            log.error(f"Reminder error {chat_id}: {e}")


async def weekly_report():
    """Понедельник 09:00 — отчёт PM в личку."""
    log.info("Weekly report triggered")
    rows = get_all_rows()
    open_rows   = [r for r in rows if r.get('status') == 'ОТКРЫТА']
    closed_rows = [r for r in rows if r.get('status') == 'ЗАКРЫТА']
    red = [r for r in open_rows if r.get('criticality') == 'RED']
    yel = [r for r in open_rows if r.get('criticality') == 'YELLOW']
    grn = [r for r in open_rows if r.get('criticality') == 'GREEN']

    by_block = {}
    for r in open_rows:
        b = r.get('block', 'Другое')
        by_block[b] = by_block.get(b, 0) + 1
    block_lines = '\n'.join(
        f'  • {b}: {n}' for b, n in sorted(by_block.items(), key=lambda x: -x[1])[:8]
    )

    text = (
        f'📊 ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ — {today_str()}\n{"═"*30}\n\n'
        f'Всего вопросов: {len(rows)}\n'
        f'⏳ Открытых: {len(open_rows)}\n'
        f'✅ Закрытых: {len(closed_rows)}\n\n'
        f'По критичности:\n🔴 RED: {len(red)}  🟡 YELLOW: {len(yel)}  🟢 GREEN: {len(grn)}\n'
    )
    if block_lines:
        text += f'\nПо блокам:\n{block_lines}\n'
    if red:
        text += f'\n🚨 Критичные RED:\n'
        for i, r in enumerate(red[:8]):
            text += f'{i+1}. #{r["id"]} {r.get("question","")[:50]}\n   👤 {r.get("responsible") or "нет ответственного"}\n'

    await notify_pm(text)


# ── Запуск ──────────────────────────────────────────────────────

async def main():
    scheduler = AsyncIOScheduler(timezone='Asia/Tashkent')
    scheduler.add_job(daily_reminder, CronTrigger(day_of_week='mon-fri', hour=10, minute=0))
    scheduler.add_job(weekly_report,  CronTrigger(day_of_week='mon',     hour=9,  minute=0))
    scheduler.start()

    log.info("✅ NBU Bot запущен в тихом режиме")
    await notify_pm('🤖 Бот запущен и слушает чаты в тихом режиме')

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
