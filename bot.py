# ═══════════════════════════════════════════════════════════════
# NBU Question Bot — тихий режим v3
# ═══════════════════════════════════════════════════════════════

import asyncio
import logging
import json
import re
import os
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
import gspread
import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY     = os.environ["ANTHROPIC_API_KEY"]
SHEET_ID          = os.environ["SHEET_ID"]
SHEET_NAME        = "Questions"
GROUP_CHAT_ID     = int(os.environ.get("GROUP_CHAT_ID", "-1003963999739"))
PM_CHAT_ID        = int(os.environ.get("PM_CHAT_ID", "5281759957"))
GOOGLE_TOKEN_JSON = os.environ["GOOGLE_TOKEN_JSON"]
BOARD_SHEET_ID    = os.environ.get("BOARD_SHEET_ID", "1zXNvio8ti1tpU4HkuROE9tzzPSQzLBCy_0gxvb7CYR0")

bot       = Bot(token=TELEGRAM_TOKEN)
dp        = Dispatcher()
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

def make_gspread():
    d = json.loads(GOOGLE_TOKEN_JSON)
    c = Credentials(
        token=d["token"], refresh_token=d["refresh_token"],
        token_uri=d["token_uri"], client_id=d["client_id"],
        client_secret=d["client_secret"], scopes=d["scopes"]
    )
    if c.expired:
        c.refresh(Request())
    return gspread.authorize(c)

gc       = make_gspread()
sh       = gc.open_by_key(SHEET_ID)
ws       = sh.worksheet(SHEET_NAME)
sh_board = gc.open_by_key(BOARD_SHEET_ID)
ws_board = sh_board.worksheet("questions_bank")

def get_all_rows() -> list:
    return ws.get_all_records()

def append_row(data: dict):
    headers = ws.row_values(1)
    row = [str(data.get(h, "")) for h in headers]
    ws.append_row(row, value_input_option="USER_ENTERED")

def update_row_by_id(question_id, updates: dict) -> bool:
    try:
        records = ws.get_all_records()
        headers = ws.row_values(1)
        for i, r in enumerate(records):
            if str(r.get("id", "")) == str(question_id):
                row_num = i + 2
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
    return next((r for r in get_all_rows() if str(r.get("id","")) == str(question_id)), None)

def today_str() -> str:
    now = datetime.now()
    return f"{now.day:02d}.{now.month:02d}.{str(now.year)[2:]}"

def crit_emoji(c: str) -> str:
    return {"RED":"🔴","YELLOW":"🟡","GREEN":"🟢"}.get(c,"⚪")

def check_duplicate(question_text: str) -> Optional[dict]:
    short_new = question_text.lower()[:25]
    for r in get_all_rows():
        if r.get("status") != "ОТКРЫТА":
            continue
        s = (r.get("question","")).lower()[:25]
        if len(s) > 10 and s == short_new[:len(s)]:
            return r
    return None

def ai_analyze(text: str, asked_by: str, reply_to: str = "") -> dict:
    prompt = (
        "Ты анализатор сообщений Telegram-чата банковского проекта НБУ Узбекистана (Milliy 3.0).\n\n"
        f"Сообщение: {text}\nАвтор: {asked_by}\n"
        + (f"Reply на: {reply_to}\n" if reply_to else "") +
        "\nЯвляется ли это вопросом требующим ответа и отслеживания?\n"
        "Вопрос: прямой вопрос, запрос информации/статуса/доступа, проблема требующая решения.\n"
        "НЕ вопрос: приветствия, благодарности, утверждения, ответы на чужие вопросы.\n\n"
        'Верни ТОЛЬКО JSON без markdown:\n'
        '{"is_question":true/false,"responsible":"@username если явно указан иначе null",'
        '"block":"Авторизация|Платежи|Интеграция|Документы|Доступы|Тестирование|Дизайн|Другое",'
        '"criticality":"RED|YELLOW|GREEN","impact":"одна фраза до 80 символов",'
        '"release":"номер если упомянут иначе пустая строка","question_clean":"очищенный текст"}'
    )
    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=400,
            messages=[{"role":"user","content":prompt}]
        )
        raw = re.sub(r"```json|```","", resp.content[0].text).strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"Claude analyze error: {e}")
        return {"is_question": False}

def ai_verify(question: str, answer: str) -> dict:
    close_kw = ["закрыт","готово","сделано","выполнено","ответ дан",
                "предоставлен","предоставили","ok","ок","принято",
                "исправлено","решено","дан","выполнили","сделали"]
    al = answer.lower().strip()
    for kw in close_kw:
        if kw in al:
            log.info(f"Keyword closing: {kw}")
            return {"isAnswer": True, "confidence": 0.9, "summary": answer[:80]}
    try:
        prompt = (
            "Контекст: трекер вопросов банковского проекта.\n"
            f"Вопрос: {question}\nReply: {answer}\n"
            "Является ли reply ответом или подтверждением закрытия?\n"
            'Верни ТОЛЬКО JSON: {"isAnswer": true/false, "confidence": 0.0-1.0, "summary": "резюме"}'
        )
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=150,
            messages=[{"role":"user","content":prompt}]
        )
        raw = re.sub(r"```json|```","", resp.content[0].text).strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"Claude verify error: {e}")
        return {"isAnswer": False, "confidence": 0, "summary": ""}

def sync_to_board(question_data: dict):
    """
    Правила синхронизации с questions_bank:
    - Найден дубль ОТКРЫТА + бот пишет ОТКРЫТА  → обновляем
    - Найден дубль ЗАКРЫТА + бот пишет ОТКРЫТА  → создаём новую (не трогаем закрытую)
    - Найден дубль ОТКРЫТА + бот пишет ЗАКРЫТА  → обновляем (закрываем)
    - Найден дубль ЗАКРЫТА + бот пишет ЗАКРЫТА  → игнорируем
    - Дубль не найден                            → создаём новую
    """
    try:
        board_rows = ws_board.get_all_records()
        q_text = question_data.get("question", "")
        incoming_status = question_data.get("status", "ОТКРЫТА")
        matched_idx = None
        matched_board_status = None

        if board_rows and q_text:
            candidates = [
                {"row": i+2, "q": r.get("question","")[:100], "status": r.get("status","")}
                for i, r in enumerate(board_rows)
                if r.get("question","").strip()
            ]
            if candidates:
                clist = "\n".join(f'{c["row"]}. {c["q"]}' for c in candidates[:30])
                prompt = (
                    "Найди строку наиболее похожую по смыслу (90%+) на новый вопрос.\n\n"
                    f"Новый вопрос: {q_text[:200]}\n\n"
                    f"Существующие:\n{clist}\n\n"
                    "Если совпадение >= 90% — верни ТОЛЬКО номер строки (целое число).\n"
                    "Если нет — верни 0."
                )
                try:
                    resp = ai_client.messages.create(
                        model="claude-haiku-4-5-20251001", max_tokens=10,
                        messages=[{"role":"user","content":prompt}]
                    )
                    txt = resp.content[0].text.strip()
                    first = txt.split()[0] if txt.split() else "0"
                    digits = "".join(c for c in first if c.isdigit())
                    row_num = int(digits) if digits else 0
                    if row_num >= 2:
                        matched_idx = row_num
                        matched_board_status = candidates[row_num-2].get("status","") if row_num-2 < len(candidates) else ""
                        log.info(f"Board dup at row {row_num} status={matched_board_status}")
                except Exception as e:
                    log.error(f"Board dedup error: {e}")

        # Применяем правила
        if matched_idx:
            board_is_closed = matched_board_status == "ЗАКРЫТА"
            bot_is_closed   = incoming_status == "ЗАКРЫТА"

            if board_is_closed and not bot_is_closed:
                # Борд закрыт, бот открытый → создаём новую запись
                log.info("Board closed + bot open → create new row")
                matched_idx = None  # сбрасываем чтобы пойти в ветку создания
            elif board_is_closed and bot_is_closed:
                # Оба закрыты → игнорируем
                log.info("Both closed → skip")
                return
            # иначе обновляем (board open + bot open/closed)

        headers = ws_board.row_values(1)
        fields = ["question","answer","criticality","impact","release",
                  "block","status","created_date","resolved_date"]

        if matched_idx:
            for col_name in fields:
                if col_name in headers and col_name in question_data:
                    col_idx = headers.index(col_name) + 1
                    ws_board.update_cell(matched_idx, col_idx, str(question_data.get(col_name,"")))
            log.info(f"Board row {matched_idx} updated")
        else:
            max_id = max(
                (int(r.get("id",0)) for r in board_rows if str(r.get("id","")).isdigit()),
                default=0
            )
            new_id = max_id + 1
            row = [str(new_id) if h == "id" else str(question_data.get(h,"")) for h in headers]
            ws_board.append_row(row, value_input_option="USER_ENTERED")
            log.info(f"Board new row id={new_id}")

    except Exception as e:
        log.error(f"sync_to_board error: {e}")


async def notify_pm(text: str):
    try:
        await bot.send_message(PM_CHAT_ID, text)
    except Exception as e:
        log.error(f"PM notify error: {e}")

@dp.message(F.text)
async def handle_message(msg: Message):
    text      = msg.text.strip()
    chat_id   = str(msg.chat.id)
    from_user = msg.from_user
    username  = f"@{from_user.username}" if from_user.username else None
    asked_by  = " ".join(filter(None,[username, from_user.first_name])).strip()

    has_reply = bool(msg.reply_to_message)
    reply_mid = str(msg.reply_to_message.message_id) if has_reply else "none"
    reply_txt = (msg.reply_to_message.text or "")[:40] if has_reply else ""
    log.info(f"IN from={asked_by} reply={has_reply} reply_mid={reply_mid} reply_txt='{reply_txt}' text='{text[:50]}'")

    if text.startswith("/"):
        await handle_command(msg, text, chat_id, asked_by)
        return

    if msg.reply_to_message:
        rows = get_all_rows()
        open_rows = [r for r in rows if r.get("status") == "ОТКРЫТА" and str(r.get("chat_id","")) == chat_id]
        matched = None

        matched = next((r for r in open_rows if str(r.get("msg_id","")) == reply_mid), None)
        if matched: log.info(f"Matched by msg_id: #{matched['id']}")

        if not matched and reply_txt:
            short = reply_txt.lower()[:20]
            matched = next((r for r in open_rows if short and short in (r.get("question","")).lower()), None)
            if matched: log.info(f"Matched by reply_text: #{matched['id']}")

        if not matched and len(open_rows) == 1:
            matched = open_rows[0]
            log.info(f"Matched as single open: #{matched['id']}")

        if matched:
            verdict = ai_verify(matched.get("question",""), text)
            log.info(f"Verdict: isAnswer={verdict.get('isAnswer')} confidence={verdict.get('confidence')}")
            if verdict.get("isAnswer") and verdict.get("confidence",0) >= 0.4:
                update_row_by_id(matched["id"], {
                    "answer": text, "status": "ЗАКРЫТА", "resolved_date": today_str()
                })
                sync_to_board({**matched, "answer": text, "status": "ЗАКРЫТА", "resolved_date": today_str()})
                await notify_pm(
                    f"✅ Вопрос #{matched['id']} закрыт через reply\n"
                    f"💬 {verdict.get('summary') or text[:80]}\n"
                    f"Ответил: {asked_by}"
                )
                return
            if not text.endswith("?") and not re.search(r"\bвопрос\b", text, re.I):
                log.info("Reply not an answer and not a question — skipping")
                return

    reply_to_user, reply_to_name = "", ""
    if msg.reply_to_message and msg.reply_to_message.from_user:
        rf = msg.reply_to_message.from_user
        reply_to_user = f"@{rf.username}" if rf.username else ""
        reply_to_name = " ".join(filter(None,[rf.first_name, rf.last_name])).strip()

    ai = ai_analyze(text, asked_by, reply_to_user or reply_to_name)

    if ai.get("is_question"):
        await create_question(msg, text, ai, chat_id, asked_by, reply_to_user, reply_to_name)
    else:
        await detect_answer(msg, text, chat_id, asked_by)


async def create_question(msg, text, ai, chat_id, asked_by, reply_to_user, reply_to_name):
    question_text = ai.get("question_clean") or text
    duplicate = check_duplicate(question_text)
    if duplicate:
        await notify_pm(f"♻️ Дубль: #{duplicate['id']}\n{question_text[:80]}\nАвтор: {asked_by}")
        return

    responsible, responsible_name = "", ""
    ai_resp = ai.get("responsible") or ""
    if ai_resp.startswith("@"):
        responsible = ai_resp; responsible_name = ai_resp
    elif reply_to_user:
        responsible = reply_to_user; responsible_name = reply_to_name or reply_to_user
    elif reply_to_name:
        responsible_name = reply_to_name
    else:
        m = re.search(r"@(\w+)", text)
        if m:
            responsible = responsible_name = f"@{m.group(1)}"

    qid  = int(datetime.now().timestamp() * 1000)
    crit = ai.get("criticality","YELLOW")

    qrow = {
        "id": qid, "release": ai.get("release",""), "block": ai.get("block","Другое"),
        "question": question_text, "answer": "", "criticality": crit,
        "impact": ai.get("impact",""), "created_date": today_str(), "resolved_date": "",
        "status": "ОТКРЫТА", "source": "bot", "responsible": responsible,
        "responsible_name": responsible_name, "asked_by": asked_by,
        "chat_id": chat_id, "chat_name": msg.chat.title or "Private",
        "msg_id": str(msg.message_id)
    }
    append_row(qrow)
    log.info(f"New question #{qid}: {question_text[:50]}")
    sync_to_board(qrow)

    resp_display = responsible or responsible_name or f"⚠️ /resp {qid} @username"
    await notify_pm(
        f"📋 Новый вопрос #{qid}\n"
        f"{crit_emoji(crit)} {crit} | 📦 {ai.get('block','Другое')}\n"
        f"👤 {resp_display}\n"
        f"💬 {question_text[:100]}\n"
        f"Автор: {asked_by}\n\n"
        f"/resp {qid} @username\n"
        f"/close {qid} <ответ>"
    )


async def detect_answer(msg, text, chat_id, asked_by):
    rows = get_all_rows()
    id_match = re.search(r"\b(\d{13,})\b", text)
    matched = None
    if id_match:
        matched = next((r for r in rows if str(r.get("id","")) == id_match.group(1)), None)
    if not matched:
        open_in_chat = [r for r in rows if r.get("status") == "ОТКРЫТА" and str(r.get("chat_id","")) == chat_id]
        if len(open_in_chat) == 1:
            matched = open_in_chat[0]
    if not matched:
        return
    verdict = ai_verify(matched.get("question",""), text)
    if verdict.get("isAnswer") and verdict.get("confidence",0) >= 0.75:
        update_row_by_id(matched["id"], {"answer":text,"status":"ЗАКРЫТА","resolved_date":today_str()})
        sync_to_board({**matched, "answer": text, "status": "ЗАКРЫТА", "resolved_date": today_str()})
        await notify_pm(f"✅ #{matched['id']} закрыт\n💬 {verdict.get('summary') or text[:80]}\nОтветил: {asked_by}")


async def handle_command(msg, text, chat_id, asked_by):
    m = re.match(r"^/close\s+(\d+)(?:\s+([\s\S]+))?$", text)
    if m:
        qid, ans = m.group(1), m.group(2) or "Закрыт"
        ok = update_row_by_id(int(qid), {"status":"ЗАКРЫТА","answer":ans,"resolved_date":today_str()})
        if ok:
            row = find_row_by_id(qid)
            if row:
                sync_to_board({**row, "answer": ans, "status": "ЗАКРЫТА", "resolved_date": today_str()})
        await msg.reply(f"✅ #{qid} закрыт" if ok else f"❌ #{qid} не найден")
        return

    m = re.match(r"^/answer\s+(\d+)(?:\s+([\s\S]+))?$", text)
    if m:
        qid, ans = m.group(1), m.group(2) or ""
        ok = update_row_by_id(int(qid), {"answer": ans})
        await msg.reply(f"💬 Ответ на #{qid} сохранён" if ok else f"❌ #{qid} не найден")
        return

    m = re.match(r"^/status\s+(\d+)", text)
    if m:
        row = find_row_by_id(m.group(1))
        if not row:
            await msg.reply(f"❌ #{m.group(1)} не найден"); return
        crit = row.get("criticality","")
        s = "✅" if row.get("status") == "ЗАКРЫТА" else "⏳"
        lines = [f"{s} #{row['id']}", f"📦 {row.get('block','—')}  {crit_emoji(crit)} {crit}",
                 f"👤 {row.get('responsible') or row.get('responsible_name') or 'не назначен'}",
                 f"📅 {row.get('created_date','—')}", f"🔄 {row.get('status','—')}",
                 f"📎 {row['impact']}" if row.get("impact") else "",
                 f"💬 {row['answer']}" if row.get("answer") else ""]
        await msg.reply("\n".join(l for l in lines if l))
        return

    m = re.match(r"^/resp\s+(\d+)\s+(.+)", text)
    if m:
        qid, val = m.group(1), m.group(2).strip()
        resp = val if val.startswith("@") else ""
        ok = update_row_by_id(int(qid), {"responsible":resp,"responsible_name":val})
        warn = "\n⚠️ Нет @username — тег недоступен" if not resp else ""
        await msg.reply(f"👤 #{qid}: {val}{warn}" if ok else f"❌ #{qid} не найден")
        return

    if text.strip() == "/report":
        rows = get_all_rows()
        open_rows = [r for r in rows if r.get("status") == "ОТКРЫТА"]
        if not open_rows:
            await msg.reply("✅ Открытых вопросов нет!"); return
        red=[r for r in open_rows if r.get("criticality")=="RED"]
        yel=[r for r in open_rows if r.get("criticality")=="YELLOW"]
        grn=[r for r in open_rows if r.get("criticality")=="GREEN"]
        sorted_rows = red+yel+grn+[r for r in open_rows if not r.get("criticality")]
        lines = [f"📊 Открытые: {len(open_rows)}\n🔴 {len(red)}  🟡 {len(yel)}  🟢 {len(grn)}\n"]
        for i,r in enumerate(sorted_rows[:20]):
            resp = r.get("responsible") or r.get("responsible_name") or "❓"
            lines.append(f"{i+1}. {crit_emoji(r.get('criticality',''))} {r.get('question','')[:55]}\n   → {resp}")
        await msg.reply("\n".join(lines))
        return

async def daily_reminder():
    log.info("Daily reminder triggered")
    rows = get_all_rows()
    open_rows = [r for r in rows if r.get("status") == "ОТКРЫТА"]
    if not open_rows: return
    by_chat = {}
    for r in open_rows:
        cid = str(r.get("chat_id",""))
        if not cid: continue
        if cid not in by_chat: by_chat[cid] = []
        by_chat[cid].append(r)
    for chat_id, chat_rows in by_chat.items():
        sorted_rows = (
            [r for r in chat_rows if r.get("criticality")=="RED"] +
            [r for r in chat_rows if r.get("criticality")=="YELLOW"] +
            [r for r in chat_rows if r.get("criticality")=="GREEN"] +
            [r for r in chat_rows if not r.get("criticality")]
        )
        tags = list(set(r["responsible"] for r in sorted_rows if r.get("responsible","").startswith("@")))
        tag_line = " ".join(tags) + " — ожидаем ответов:" if tags else "⚠️ Открытые вопросы:"
        red_count = sum(1 for r in sorted_rows if r.get("criticality")=="RED")
        lines = [f"🔔 Открытые вопросы\n{tag_line}\n"]
        for i,r in enumerate(sorted_rows[:15]):
            resp = r.get("responsible") or r.get("responsible_name") or "❓"
            lines.append(f"{i+1}. {crit_emoji(r.get('criticality',''))} {r.get('question','')[:60]}\n   → {resp}")
        lines.append(f"\n🔴 Критичных: {red_count} из {len(sorted_rows)}")
        try:
            await bot.send_message(int(chat_id), "\n".join(lines))
        except Exception as e:
            log.error(f"Reminder error: {e}")

async def weekly_report():
    log.info("Weekly report triggered")
    rows = get_all_rows()
    open_rows   = [r for r in rows if r.get("status")=="ОТКРЫТА"]
    closed_rows = [r for r in rows if r.get("status")=="ЗАКРЫТА"]
    red=[r for r in open_rows if r.get("criticality")=="RED"]
    yel=[r for r in open_rows if r.get("criticality")=="YELLOW"]
    grn=[r for r in open_rows if r.get("criticality")=="GREEN"]
    by_block = {}
    for r in open_rows:
        b = r.get("block","Другое"); by_block[b] = by_block.get(b,0)+1
    block_lines = "\n".join(f"  • {b}: {n}" for b,n in sorted(by_block.items(),key=lambda x:-x[1])[:8])
    text = (f"📊 ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ — {today_str()}\n{'═'*30}\n\n"
            f"Всего: {len(rows)}  ⏳ Открытых: {len(open_rows)}  ✅ Закрытых: {len(closed_rows)}\n"
            f"🔴 RED: {len(red)}  🟡 YELLOW: {len(yel)}  🟢 GREEN: {len(grn)}\n")
    if block_lines: text += f"\nПо блокам:\n{block_lines}\n"
    if red:
        text += "\n🚨 Критичные RED:\n"
        for i,r in enumerate(red[:8]):
            text += f"{i+1}. #{r['id']} {r.get('question','')[:50]}\n   👤 {r.get('responsible') or 'нет ответственного'}\n"
    await notify_pm(text)

async def main():
    scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")
    scheduler.add_job(daily_reminder, CronTrigger(day_of_week="mon-fri", hour=10, minute=0))
    scheduler.add_job(weekly_report,  CronTrigger(day_of_week="mon",     hour=9,  minute=0))
    scheduler.start()
    log.info("✅ NBU Bot v4 запущен")
    await notify_pm("🤖 Бот v4 запущен — синхронизация с борд включена")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
