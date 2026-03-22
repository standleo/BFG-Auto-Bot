#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BFG AUTO PLATFORM v3.0
pip install flask telethon apscheduler requests
python app.py  →  http://localhost:5000
"""

import os, json, sqlite3, asyncio, threading, time, requests, hashlib, secrets
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, Response, session

# ── ПУТИ ───────────────────────────────────────────
BASE = "/storage/emulated/0/BFGAutoBotSQLite"
if not os.path.exists("/storage/emulated/0"):
    BASE = os.path.join(os.path.expanduser("~"), "BFGAutoBotSQLite")
os.makedirs(BASE, exist_ok=True)
DB  = os.path.join(BASE, "bfg.db")
BOT = "@bfgproject"
ADMIN_API_ID = "33050395"

# ── УВЕДОМЛЕНИЯ ────────────────────────────────────
TG_TOKEN = "8639300142:AAGZ9A7FGD3S997ZSrR9OpYp0IL5zit8wuw"

def tg_send(chat_id, text):
    if not chat_id: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        print(f"TG notify error: {e}")

def get_setting(key, default=""):
    try:
        conn = db_conn(); r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close(); return r["value"] if r else default
    except: return default

def notify(text):
    chat_id = get_setting("notify_chat_id")
    enabled  = get_setting("notify_enabled", "0")
    print(f"[notify] chat_id={chat_id!r} enabled={enabled} text={text[:50]!r}")
    if chat_id and enabled == "1":
        threading.Thread(target=tg_send, args=(chat_id, text), daemon=True).start()

# ── FLASK ───────────────────────────────────────────
app = Flask(__name__)
app.secret_key = "bfg_x9k_secret_2024"
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# ── DATABASE ────────────────────────────────────────
def db_conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    c = db_conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS accounts (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        phone          TEXT UNIQUE NOT NULL,
        api_id         TEXT NOT NULL,
        api_hash       TEXT NOT NULL,
        session_string TEXT DEFAULT '',
        tg_name        TEXT DEFAULT '',
        is_active      INTEGER DEFAULT 0,
        approved       INTEGER DEFAULT 1,
        created_at     TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id   INTEGER NOT NULL,
        command_text TEXT NOT NULL,
        btn_keyword  TEXT DEFAULT '',
        delay_value  INTEGER DEFAULT 1,
        delay_unit   TEXT DEFAULT 'hours',
        repeat_type  TEXT DEFAULT 'infinite',
        repeat_n     INTEGER DEFAULT 1,
        run_count    INTEGER DEFAULT 0,
        is_active    INTEGER DEFAULT 1,
        last_run     TEXT DEFAULT '',
        next_run     TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER,
        command    TEXT,
        result     TEXT DEFAULT '',
        buttons    TEXT DEFAULT '[]',
        status     TEXT DEFAULT 'ok',
        timestamp  TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    );
    CREATE TABLE IF NOT EXISTS user_sessions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ip         TEXT,
        user_agent TEXT,
        login_time TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS stats_daily (
        date                TEXT PRIMARY KEY,
        total_commands      INTEGER DEFAULT 0,
        successful_commands INTEGER DEFAULT 0,
        failed_commands     INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS pending_registrations (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        phone     TEXT,
        api_id    TEXT,
        api_hash  TEXT,
        ip        TEXT,
        timestamp TEXT DEFAULT (datetime('now','localtime')),
        approved  INTEGER DEFAULT 0
    );
    INSERT OR IGNORE INTO settings VALUES ('auto_enabled','1');
    INSERT OR IGNORE INTO settings VALUES ('check_interval','60');
    INSERT OR IGNORE INTO settings VALUES ('notify_chat_id','');
    INSERT OR IGNORE INTO settings VALUES ('notify_enabled','0');
    CREATE TABLE IF NOT EXISTS trades (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER,
        action    TEXT,
        price     TEXT DEFAULT '',
        amount    TEXT DEFAULT '',
        result    TEXT DEFAULT '',
        status    TEXT DEFAULT 'ok',
        timestamp TEXT DEFAULT (datetime('now','localtime'))
    );
    INSERT OR IGNORE INTO settings VALUES ('trade_enabled','0');
    INSERT OR IGNORE INTO settings VALUES ('trade_buy_below','0');
    INSERT OR IGNORE INTO settings VALUES ('trade_sell_above','0');
    INSERT OR IGNORE INTO settings VALUES ('trade_amount','100');
    INSERT OR IGNORE INTO settings VALUES ('trade_interval','5');
    CREATE TABLE IF NOT EXISTS site_users (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        nickname   TEXT UNIQUE NOT NULL,
        password   TEXT NOT NULL,
        account_id INTEGER DEFAULT 0,
        is_admin   INTEGER DEFAULT 0,
        is_banned  INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS banned_phones (
        phone  TEXT PRIMARY KEY,
        reason TEXT DEFAULT '',
        at     TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS banned_ips (
        ip     TEXT PRIMARY KEY,
        reason TEXT DEFAULT '',
        at     TEXT DEFAULT (datetime('now','localtime'))
    );
    INSERT OR IGNORE INTO settings VALUES ('notify_registrations','0');
    INSERT OR IGNORE INTO settings VALUES ('notify_commands','0');
    INSERT OR IGNORE INTO settings VALUES ('theme','red');
    """)
    c.commit(); c.close()

# ── STAT HELPERS ────────────────────────────────────
def stat_inc(status):
    today = datetime.now().strftime("%Y-%m-%d")
    c = db_conn()
    c.execute("INSERT OR IGNORE INTO stats_daily(date) VALUES(?)", (today,))
    c.execute("UPDATE stats_daily SET total_commands=total_commands+1 WHERE date=?", (today,))
    if status == "ok":
        c.execute("UPDATE stats_daily SET successful_commands=successful_commands+1 WHERE date=?", (today,))
    else:
        c.execute("UPDATE stats_daily SET failed_commands=failed_commands+1 WHERE date=?", (today,))
    c.commit(); c.close()

def hash_pw(p): return hashlib.sha256(p.encode('utf-8')).hexdigest()
def get_ip(): return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()

# ── TELETHON ─────────────────────────────────────────
AUTH_TMP  = {}
CLIENTS   = {}
CMD_CACHE = {}

# ── ОДИН постоянный event loop для всего Telethon ──
# Это исправляет ошибку "event loop must not change after connection"
_TG_LOOP = asyncio.new_event_loop()

def _start_tg_loop():
    asyncio.set_event_loop(_TG_LOOP)
    _TG_LOOP.run_forever()

threading.Thread(target=_start_tg_loop, daemon=True).start()

def run_async(coro, timeout=25):
    """Запускает корутину в постоянном TG-loop из любого потока."""
    fut = asyncio.run_coroutine_threadsafe(coro, _TG_LOOP)
    try:
        return {"v": fut.result(timeout=timeout)}
    except Exception as e:
        return {"e": str(e)}

async def get_client(aid):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    if aid in CLIENTS:
        c = CLIENTS[aid]
        if not c.is_connected(): await c.connect()
        if await c.is_user_authorized(): return c
    c2 = db_conn(); row = c2.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone(); c2.close()
    if not row or not row["session_string"]: return None
    try:
        c = TelegramClient(StringSession(row["session_string"]), int(row["api_id"]), row["api_hash"])
        await c.connect()
        if await c.is_user_authorized(): CLIENTS[aid] = c; return c
    except: pass
    return None

async def get_btns(msg):
    try:
        rows = await msg.get_buttons()
        return [b.text for row in rows for b in row] if rows else []
    except: return []

async def click_kw(msg, kw):
    try:
        rows = await msg.get_buttons()
        if not rows: return False
        for row in rows:
            for b in row:
                if kw.lower() in b.text.lower(): await b.click(); return True
    except: pass
    return False

# ── AUTH ─────────────────────────────────────────────
@app.before_request
def log_visit():
    if request.path == "/" and request.method == "GET":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
        ua = request.user_agent.string or ""
        c = db_conn()
        c.execute("INSERT INTO user_sessions(ip,user_agent) VALUES(?,?)", (ip, ua[:300]))
        c.commit(); c.close()

@app.route("/api/auth/start", methods=["POST"])
def auth_start():
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    d = request.json or {}
    api_id, api_hash, phone = str(d.get("api_id","")).strip(), str(d.get("api_hash","")).strip(), str(d.get("phone","")).strip()
    if not all([api_id, api_hash, phone]):
        return jsonify({"error": "Заполни все поля"}), 400
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")

    c = db_conn()
    row = c.execute("SELECT id FROM accounts WHERE phone=?", (phone,)).fetchone()
    if row:
        aid = row["id"]; c.execute("UPDATE accounts SET api_id=?,api_hash=? WHERE id=?", (api_id, api_hash, aid))
    else:
        c.execute("INSERT INTO accounts(phone,api_id,api_hash) VALUES(?,?,?)", (phone, api_id, api_hash))
        aid = c.execute("SELECT id FROM accounts WHERE phone=?", (phone,)).fetchone()["id"]
        # pending
        c.execute("INSERT INTO pending_registrations(phone,api_id,api_hash,ip) VALUES(?,?,?,?)", (phone, api_id, api_hash, ip))
        c.commit(); c.close()
        # уведомление
        if get_setting("notify_registrations","0") == "1":
            ts = datetime.now().strftime("%d.%m.%Y %H:%M")
            notify(f"🔔 <b>НОВАЯ РЕГИСТРАЦИЯ</b>\n📱 Телефон: {phone}\n🌐 IP: {ip}\n🕐 {ts}\n✅ /approve_{aid}\n❌ /decline_{aid}")
    if not row:
        c = db_conn()
    c.commit(); c.close()

    async def _start():
        cl = TelegramClient(StringSession(), int(api_id), api_hash)
        await cl.connect()
        if await cl.is_user_authorized():
            me = await cl.get_me(); sess = cl.session.save()
            name = f"{me.first_name or ''} {me.last_name or ''}".strip()
            c2 = db_conn(); c2.execute("UPDATE accounts SET session_string=?,tg_name=? WHERE id=?", (sess,name,aid)); c2.commit(); c2.close()
            CLIENTS[aid] = cl
            return {"state":"done","name":name,"account_id":aid}
        sent = await cl.send_code_request(phone)
        AUTH_TMP[phone] = {"client":cl,"hash":sent.phone_code_hash,"acc_id":aid}
        return {"state":"code_sent"}

    r = run_async(_start())
    if "e" in r: return jsonify({"error": r["e"]}), 400
    return jsonify({"ok":True,"account_id":aid,**r.get("v",{})})

@app.route("/api/auth/verify", methods=["POST"])
def auth_verify():
    d = request.json or {}
    phone, code, pwd = str(d.get("phone","")).strip(), str(d.get("code","")).strip(), str(d.get("password",""))
    tmp = AUTH_TMP.get(phone)
    if not tmp: return jsonify({"error":"Сессия истекла"}), 400

    async def _verify():
        cl = tmp["client"]; aid = tmp["acc_id"]
        c = db_conn(); row = c.execute("SELECT phone FROM accounts WHERE id=?", (aid,)).fetchone(); c.close()

        # Если уже прошли шаг кода — только пароль 2FA
        if tmp.get("need_2fa"):
            if not pwd:
                return {"state": "need_2fa"}
            await cl.sign_in(password=pwd)
        elif code:
            try:
                await cl.sign_in(row["phone"], code, phone_code_hash=tmp["hash"])
            except Exception as e:
                # Ловим 2FA по строке — надёжно в любом event loop
                err = str(type(e).__name__) + str(e)
                if "SessionPasswordNeeded" in err or "password is required" in err or "Two-steps" in err:
                    AUTH_TMP[phone]["need_2fa"] = True
                    if pwd:
                        # пароль уже введён вместе с кодом
                        await cl.sign_in(password=pwd)
                    else:
                        return {"state": "need_2fa"}
                else:
                    raise
        elif pwd:
            await cl.sign_in(password=pwd)
        else:
            return {"state": "need_2fa"}

        me = await cl.get_me(); sess = cl.session.save()
        name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        c2 = db_conn(); c2.execute("UPDATE accounts SET session_string=?,tg_name=? WHERE id=?", (sess,name,aid)); c2.commit(); c2.close()
        CLIENTS[aid] = cl
        AUTH_TMP.pop(phone, None)
        return {"state": "done", "name": name, "account_id": aid}

    r = run_async(_verify())
    if "e" in r: return jsonify({"error":r["e"]}), 400
    return jsonify({"ok":True,**r.get("v",{})})

# ── ACCOUNTS ─────────────────────────────────────────
@app.route("/api/accounts")
def list_accounts():
    c = db_conn(); rows = c.execute("SELECT id,phone,tg_name,is_active,session_string,approved FROM accounts ORDER BY id DESC").fetchall(); c.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/accounts/active", methods=["POST"])
def set_active():
    aid = int((request.json or {}).get("id",0))
    c = db_conn(); c.execute("UPDATE accounts SET is_active=0"); c.execute("UPDATE accounts SET is_active=1 WHERE id=?", (aid,)); c.commit(); c.close()
    return jsonify({"ok":True})

@app.route("/api/accounts/<int:aid>", methods=["DELETE"])
def del_account(aid):
    c = db_conn()
    for t in ["accounts","tasks","logs"]: c.execute(f"DELETE FROM {t} WHERE {'id' if t=='accounts' else 'account_id'}=?", (aid,))
    c.commit(); c.close(); CLIENTS.pop(aid,None)
    return jsonify({"ok":True})

# ── APPROVE/DECLINE via TG webhook ──────────────────
@app.route("/api/tg_webhook", methods=["POST"])
def tg_webhook():
    d = request.json or {}
    msg = d.get("message",{})
    text = msg.get("text","")
    if text.startswith("/approve_"):
        aid = int(text.split("_")[1])
        c = db_conn(); c.execute("UPDATE accounts SET approved=1 WHERE id=?", (aid,)); c.commit(); c.close()
        return jsonify({"ok":True,"action":"approved"})
    if text.startswith("/decline_"):
        aid = int(text.split("_")[1])
        c = db_conn(); c.execute("DELETE FROM accounts WHERE id=?", (aid,)); c.commit(); c.close()
        return jsonify({"ok":True,"action":"declined"})
    return jsonify({"ok":True})

# ── COMMAND CACHE ───────────────────────────────────
CACHE_CMDS = {"Б/Баланс","Профиль","Рейтинг","Биткоин курс"}
CACHE_TTL  = 30  # секунд

def cache_get(aid, cmd):
    key = f"{aid}:{cmd}"
    if key in CMD_CACHE:
        ts, val = CMD_CACHE[key]
        if time.time() - ts < CACHE_TTL: return val
    return None

def cache_set(aid, cmd, val):
    if cmd in CACHE_CMDS:
        CMD_CACHE[f"{aid}:{cmd}"] = (time.time(), val)

# ── COMMAND RUN ──────────────────────────────────────
@app.route("/api/command/run", methods=["POST"])
def run_command():
    d = request.json or {}
    aid = int(d.get("account_id",0)); cmd = str(d.get("command_text","")).strip()
    if not cmd: return jsonify({"error":"Нет команды"}), 400

    # Кэш
    cached = cache_get(aid, cmd)
    if cached: return jsonify({"ok":True,"cached":True,**cached})

    async def _run():
        cl = await get_client(aid)
        if not cl: raise Exception("Нет авторизованной сессии. Войди в аккаунт.")
        await cl.send_message(BOT, cmd)
        await asyncio.sleep(0.5)
        msgs = await cl.get_messages(BOT, limit=1)
        if not msgs: return {"text":"","buttons":[]}
        m = msgs[0]; text = m.text or ""; btns = await get_btns(m)
        return {"text":text,"buttons":btns}

    r = run_async(_run(), timeout=20)
    c = db_conn()
    if "e" in r:
        c.execute("INSERT INTO logs(account_id,command,result,status) VALUES(?,?,?,?)", (aid,cmd,r["e"][:300],"error"))
        c.commit(); c.close(); stat_inc("error")
        # уведомление об ошибке
        if get_setting("notify_commands","0") == "1":
            acc_row = db_conn().execute("SELECT phone FROM accounts WHERE id=?", (aid,)).fetchone()
            ts = datetime.now().strftime("%d.%m %H:%M")
            notify(f"❌ <b>ОШИБКА</b>\n👤 {acc_row['phone'] if acc_row else aid}\n⚡ {cmd}\n🔥 {r['e'][:200]}\n🕐 {ts}")
        return jsonify({"error":r["e"]}), 400
    val = r.get("v",{})
    c.execute("INSERT INTO logs(account_id,command,result,buttons) VALUES(?,?,?,?)",
              (aid,cmd,(val.get("text",""))[:1000], json.dumps(val.get("buttons",[]),ensure_ascii=False)))
    c.commit(); c.close(); stat_inc("ok")
    cache_set(aid, cmd, val)
    # уведомление об успехе
    if get_setting("notify_commands","0") == "1":
        acc_row2 = db_conn().execute("SELECT phone FROM accounts WHERE id=?", (aid,)).fetchone()
        ts2 = datetime.now().strftime("%d.%m %H:%M")
        res_short = (val.get("text",""))[:100]
        notify(f"✅ <b>ВЫПОЛНЕНА КОМАНДА</b>\n👤 {acc_row2['phone'] if acc_row2 else aid}\n⚡ {cmd}\n📊 {res_short}\n🕐 {ts2}")
    return jsonify({"ok":True,**val})

@app.route("/api/command/click", methods=["POST"])
def click_btn_api():
    d = request.json or {}; aid = int(d.get("account_id",0)); kw = str(d.get("keyword","")).strip()

    async def _click():
        cl = await get_client(aid)
        if not cl: raise Exception("Нет сессии")
        msgs = await cl.get_messages(BOT, limit=1)
        if not msgs: raise Exception("Нет сообщений от бота")
        ok = await click_kw(msgs[0], kw)
        if not ok:
            btns = await get_btns(msgs[0]); raise Exception(f"'{kw}' не найдена. Есть: {btns}")
        await asyncio.sleep(0.5)
        msgs2 = await cl.get_messages(BOT, limit=1)
        if not msgs2: return {"text":"","buttons":[]}
        text = msgs2[0].text or ""; btns2 = await get_btns(msgs2[0])
        c = db_conn()
        c.execute("INSERT INTO logs(account_id,command,result,buttons) VALUES(?,?,?,?)",
                  (aid,f"[кнопка] {kw}",text[:1000],json.dumps(btns2,ensure_ascii=False)))
        c.commit(); c.close(); stat_inc("ok")
        return {"text":text,"buttons":btns2}

    r = run_async(_click())
    if "e" in r: return jsonify({"error":r["e"]}), 400
    return jsonify({"ok":True,**r.get("v",{})})

@app.route("/api/commands")
def list_commands():
    return jsonify(COMMANDS_LIST)

# ── TASKS ───────────────────────────────────────────
def isec(val,unit): return max(60,int(val))*{"minutes":60,"hours":3600,"days":86400}.get(unit,3600)

@app.route("/api/tasks")
def list_tasks():
    c = db_conn()
    rows = c.execute("SELECT t.*,a.phone,a.tg_name FROM tasks t LEFT JOIN accounts a ON t.account_id=a.id ORDER BY t.id DESC").fetchall()
    c.close(); return jsonify([dict(r) for r in rows])

@app.route("/api/tasks", methods=["POST"])
def create_task():
    d = request.json or {}
    nxt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c = db_conn()
    c.execute("INSERT INTO tasks(account_id,command_text,btn_keyword,delay_value,delay_unit,repeat_type,repeat_n,next_run) VALUES(?,?,?,?,?,?,?,?)",
              (d.get("account_id"),d.get("command_text",""),d.get("btn_keyword",""),
               d.get("delay_value",1),d.get("delay_unit","hours"),d.get("repeat_type","infinite"),
               int(d.get("repeat_n",1)),nxt))
    c.commit(); c.close(); return jsonify({"ok":True})

@app.route("/api/tasks/<int:tid>", methods=["DELETE"])
def del_task(tid):
    c = db_conn(); c.execute("DELETE FROM tasks WHERE id=?", (tid,)); c.commit(); c.close(); return jsonify({"ok":True})

@app.route("/api/tasks/<int:tid>/toggle", methods=["POST"])
def toggle_task(tid):
    c = db_conn(); row = c.execute("SELECT is_active FROM tasks WHERE id=?", (tid,)).fetchone()
    nv = 0 if row["is_active"] else 1
    c.execute("UPDATE tasks SET is_active=? WHERE id=?", (nv,tid)); c.commit(); c.close()
    return jsonify({"ok":True,"is_active":nv})

@app.route("/api/tasks/<int:tid>/run", methods=["POST"])
def run_task_now(tid):
    c = db_conn(); t = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone(); c.close()
    if not t: return jsonify({"error":"Не найдено"}),404
    threading.Thread(target=exec_task, args=(dict(t),), daemon=True).start()
    return jsonify({"ok":True})

def exec_task(t):
    async def _run():
        cl = await get_client(t["account_id"])
        if not cl: return
        await cl.send_message(BOT, t["command_text"]); await asyncio.sleep(0.5)
        msgs = await cl.get_messages(BOT, limit=1)
        text, btns = "", []
        if msgs:
            m = msgs[0]; text = m.text or ""; btns = await get_btns(m)
            if t.get("btn_keyword"):
                ok = await click_kw(m, t["btn_keyword"])
                if ok:
                    await asyncio.sleep(0.5)
                    msgs2 = await cl.get_messages(BOT, limit=1)
                    if msgs2: text = msgs2[0].text or ""; btns = await get_btns(msgs2[0])
        nxt = (datetime.now()+timedelta(seconds=isec(t["delay_value"],t["delay_unit"]))).strftime("%Y-%m-%d %H:%M:%S")
        nc = t["run_count"]+1; na = t["is_active"]
        if t["repeat_type"] == "once": na = 0
        elif t["repeat_type"] == "n_times" and nc >= t["repeat_n"]: na = 0
        c = db_conn()
        c.execute("UPDATE tasks SET run_count=?,last_run=datetime('now','localtime'),next_run=?,is_active=? WHERE id=?", (nc,nxt,na,t["id"]))
        c.execute("INSERT INTO logs(account_id,command,result,buttons) VALUES(?,?,?,?)",
                  (t["account_id"],t["command_text"],text[:1000],json.dumps(btns,ensure_ascii=False)))
        c.commit(); c.close(); stat_inc("ok")
    run_async(_run())

# ── LOGS ────────────────────────────────────────────
@app.route("/api/logs")
def get_logs():
    c = db_conn()
    rows = c.execute("SELECT l.*,a.phone,a.tg_name FROM logs l LEFT JOIN accounts a ON l.account_id=a.id ORDER BY l.id DESC LIMIT 500").fetchall()
    c.close(); return jsonify([dict(r) for r in rows])

@app.route("/api/logs/clear", methods=["POST"])
def clear_logs():
    c = db_conn(); c.execute("DELETE FROM logs"); c.commit(); c.close(); return jsonify({"ok":True})

# ── SETTINGS ────────────────────────────────────────
@app.route("/api/settings")
def get_settings():
    c = db_conn(); rows = c.execute("SELECT key,value FROM settings").fetchall(); c.close()
    return jsonify({r["key"]:r["value"] for r in rows})

@app.route("/api/settings", methods=["POST"])
def save_settings():
    c = db_conn()
    for k,v in (request.json or {}).items():
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k,str(v)))
    c.commit(); c.close(); return jsonify({"ok":True})

@app.route("/api/settings/dbpath")
def dbpath():
    return jsonify({"path":DB})

# ── STATISTICS ──────────────────────────────────────
@app.route("/api/stats/daily")
def stats_daily():
    c = db_conn()
    rows = c.execute("SELECT * FROM stats_daily ORDER BY date DESC LIMIT 30").fetchall()
    total = c.execute("SELECT COUNT(*) as cnt FROM logs").fetchone()["cnt"]
    ok    = c.execute("SELECT COUNT(*) as cnt FROM logs WHERE status='ok'").fetchone()["cnt"]
    err   = c.execute("SELECT COUNT(*) as cnt FROM logs WHERE status='error'").fetchone()["cnt"]
    top   = c.execute("SELECT command, COUNT(*) as cnt FROM logs GROUP BY command ORDER BY cnt DESC LIMIT 10").fetchall()
    sessions = c.execute("SELECT COUNT(*) as cnt FROM user_sessions").fetchone()["cnt"]
    c.close()
    return jsonify({
        "daily":   [dict(r) for r in rows],
        "total":   total,
        "ok":      ok,
        "errors":  err,
        "top_cmds":[dict(r) for r in top],
        "sessions":sessions,
    })

@app.route("/api/stats/sessions")
def stats_sessions():
    c = db_conn()
    rows = c.execute("SELECT * FROM user_sessions ORDER BY id DESC LIMIT 100").fetchall()
    c.close(); return jsonify([dict(r) for r in rows])

# ── SCHEDULER ───────────────────────────────────────
_trade_last_tick = [0]

def scheduler_loop():
    while True:
        try:
            if get_setting("auto_enabled","1") == "1":
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                c = db_conn()
                tasks = c.execute("SELECT * FROM tasks WHERE is_active=1 AND next_run<=?", (now,)).fetchall()
                c.close()
                for t in tasks:
                    threading.Thread(target=exec_task, args=(dict(t),), daemon=True).start()
            # Трейдинг тик
            try:
                interval = int(get_setting("trade_interval","5") or 5) * 60
                if time.time() - _trade_last_tick[0] >= interval:
                    _trade_last_tick[0] = time.time()
                    threading.Thread(target=trading_tick, daemon=True).start()
            except Exception as te:
                print(f"Trade tick: {te}")
        except Exception as e:
            print(f"Scheduler: {e}")
        time.sleep(30)


# ══════════════════════════════════════════════════════
# SITE AUTH (никнейм + пароль поверх Telegram-аккаунта)
# ══════════════════════════════════════════════════════

def current_user():
    uid = session.get("su_id")
    if not uid: return None
    c = db_conn(); u = c.execute("SELECT * FROM site_users WHERE id=?", (uid,)).fetchone(); c.close()
    return dict(u) if u else None

def require_admin_check():
    u = current_user()
    return u and u["is_admin"] == 1

@app.route("/api/site/register", methods=["POST"])
def site_register():
    d = request.json or {}
    nick   = str(d.get("nickname","")).strip()
    pw     = str(d.get("password","")).strip()
    acc_id = int(d.get("account_id", 0))
    if not nick or not pw:
        return jsonify({"error":"Заполни никнейм и пароль"}), 400
    if len(pw) < 4:
        return jsonify({"error":"Пароль минимум 4 символа"}), 400
    ip = get_ip()
    c = db_conn()
    if c.execute("SELECT id FROM banned_ips WHERE ip=?", (ip,)).fetchone():
        c.close(); return jsonify({"error":"IP заблокирован"}), 403
    if c.execute("SELECT id FROM site_users WHERE nickname=?", (nick,)).fetchone():
        c.close(); return jsonify({"error":"Никнейм занят"}), 400
    # Проверить: если api_id совпадает с ADMIN_API_ID — это владелец
    is_admin = 0
    if acc_id:
        row = c.execute("SELECT api_id,tg_name FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if row and str(row["api_id"]).strip() == ADMIN_API_ID:
            is_admin = 1
    c.execute("INSERT INTO site_users(nickname,password,account_id,is_admin) VALUES(?,?,?,?)",
              (nick, hash_pw(pw), acc_id, is_admin))
    uid = c.execute("SELECT id FROM site_users WHERE nickname=?", (nick,)).fetchone()["id"]
    # Если это владелец — автоматически получить его Telegram ID как notify_chat_id
    if is_admin and acc_id:
        async def _get_tg_id():
            cl = await get_client(acc_id)
            if not cl: return None
            me = await cl.get_me()
            return me.id if me else None
        r_id = run_async(_get_tg_id())
        tg_id = r_id.get("v") if r_id else None
        if tg_id:
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('notify_chat_id',?)", (str(tg_id),))
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('notify_enabled','1')")
            print(f"[admin] notify_chat_id auto-set to {tg_id}")
    c.commit(); c.close()
    session["su_id"] = uid
    # Уведомить владельца о новой регистрации (если не сам владелец)
    if not is_admin:
        if get_setting("notify_registrations","0") == "1":
            ts = datetime.now().strftime("%d.%m.%Y %H:%M")
            notify(f"🔔 <b>НОВАЯ РЕГИСТРАЦИЯ</b>\n👤 Никнейм: {nick}\n🌐 IP: {ip}\n🕐 {ts}")
    return jsonify({"ok":True, "user_id":uid, "nickname":nick, "is_admin":is_admin})

@app.route("/api/site/login", methods=["POST"])
def site_login():
    d = request.json or {}
    nick = str(d.get("nickname","")).strip()
    pw   = str(d.get("password","")).strip()
    if not nick or not pw:
        return jsonify({"error":"Введи никнейм и пароль"}), 400
    c = db_conn()
    u = c.execute("SELECT * FROM site_users WHERE nickname=?", (nick,)).fetchone()
    c.close()
    if not u: return jsonify({"error":"Пользователь не найден"}), 404
    if u["is_banned"]: return jsonify({"error":"Аккаунт заблокирован"}), 403
    if u["password"] != hash_pw(pw): return jsonify({"error":"Неверный пароль"}), 401
    session["su_id"] = u["id"]
    return jsonify({"ok":True, "user_id":u["id"], "nickname":u["nickname"], "is_admin":u["is_admin"], "account_id":u["account_id"]})

@app.route("/api/site/logout", methods=["POST"])
def site_logout():
    session.pop("su_id", None)
    return jsonify({"ok":True})

@app.route("/api/site/me")
def site_me():
    u = current_user()
    if not u: return jsonify({"error":"not_logged_in"}), 401
    return jsonify({k:u[k] for k in ["id","nickname","is_admin","is_banned","created_at","account_id"]})

@app.route("/api/site/profiles")
def site_profiles():
    c = db_conn()
    rows = c.execute("""
        SELECT su.id, su.nickname, su.is_admin, su.is_banned, su.created_at,
               a.tg_name, a.phone,
               (SELECT COUNT(*) FROM tasks WHERE account_id=su.account_id) as tasks_cnt,
               (SELECT COUNT(*) FROM logs  WHERE account_id=su.account_id) as logs_cnt
        FROM site_users su LEFT JOIN accounts a ON su.account_id=a.id
        ORDER BY su.is_admin DESC, su.id ASC
    """).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

# ══════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════

@app.route("/api/admin/users")
def admin_users():
    if not require_admin_check(): return jsonify({"error":"Нет доступа"}), 403
    c = db_conn()
    rows = c.execute("""
        SELECT su.*, a.phone, a.tg_name,
               (SELECT COUNT(*) FROM tasks WHERE account_id=su.account_id) as tasks_cnt,
               (SELECT COUNT(*) FROM logs  WHERE account_id=su.account_id) as logs_cnt
        FROM site_users su LEFT JOIN accounts a ON su.account_id=a.id
        ORDER BY su.is_admin DESC, su.id ASC
    """).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/ban/phone", methods=["POST"])
def admin_ban_phone():
    if not require_admin_check(): return jsonify({"error":"Нет доступа"}), 403
    d = request.json or {}
    phone = str(d.get("phone","")).strip()
    reason = str(d.get("reason",""))
    if not phone: return jsonify({"error":"Укажи номер"}), 400
    c = db_conn()
    c.execute("INSERT OR REPLACE INTO banned_phones(phone,reason) VALUES(?,?)", (phone, reason))
    c.execute("UPDATE site_users SET is_banned=1 WHERE account_id IN (SELECT id FROM accounts WHERE phone=?)", (phone,))
    c.commit(); c.close()
    return jsonify({"ok":True})

@app.route("/api/admin/unban/phone", methods=["POST"])
def admin_unban_phone():
    if not require_admin_check(): return jsonify({"error":"Нет доступа"}), 403
    phone = str((request.json or {}).get("phone","")).strip()
    c = db_conn()
    c.execute("DELETE FROM banned_phones WHERE phone=?", (phone,))
    c.execute("UPDATE site_users SET is_banned=0 WHERE account_id IN (SELECT id FROM accounts WHERE phone=?)", (phone,))
    c.commit(); c.close()
    return jsonify({"ok":True})

@app.route("/api/admin/ban/ip", methods=["POST"])
def admin_ban_ip():
    if not require_admin_check(): return jsonify({"error":"Нет доступа"}), 403
    d = request.json or {}
    ip = str(d.get("ip","")).strip(); reason = str(d.get("reason",""))
    if not ip: return jsonify({"error":"Укажи IP"}), 400
    c = db_conn(); c.execute("INSERT OR REPLACE INTO banned_ips(ip,reason) VALUES(?,?)", (ip,reason)); c.commit(); c.close()
    return jsonify({"ok":True})

@app.route("/api/admin/unban/ip", methods=["POST"])
def admin_unban_ip():
    if not require_admin_check(): return jsonify({"error":"Нет доступа"}), 403
    ip = str((request.json or {}).get("ip","")).strip()
    c = db_conn(); c.execute("DELETE FROM banned_ips WHERE ip=?", (ip,)); c.commit(); c.close()
    return jsonify({"ok":True})

@app.route("/api/admin/delete/user", methods=["POST"])
def admin_delete_user():
    if not require_admin_check(): return jsonify({"error":"Нет доступа"}), 403
    target = int((request.json or {}).get("user_id",0))
    me = current_user()
    if me and target == me["id"]: return jsonify({"error":"Нельзя удалить себя"}), 400
    c = db_conn()
    u = c.execute("SELECT account_id FROM site_users WHERE id=?", (target,)).fetchone()
    if u:
        aid = u["account_id"]
        c.execute("DELETE FROM site_users WHERE id=?", (target,))
        if aid:
            c.execute("DELETE FROM tasks WHERE account_id=?", (aid,))
            c.execute("DELETE FROM logs  WHERE account_id=?", (aid,))
            c.execute("DELETE FROM accounts WHERE id=?", (aid,))
    c.commit(); c.close()
    return jsonify({"ok":True})

@app.route("/api/admin/banned")
def admin_banned():
    if not require_admin_check(): return jsonify({"error":"Нет доступа"}), 403
    c = db_conn()
    phones = c.execute("SELECT * FROM banned_phones ORDER BY at DESC").fetchall()
    ips    = c.execute("SELECT * FROM banned_ips ORDER BY at DESC").fetchall()
    visits = c.execute("SELECT * FROM user_sessions ORDER BY id DESC LIMIT 50").fetchall()
    c.close()
    return jsonify({"banned_phones":[dict(r) for r in phones], "banned_ips":[dict(r) for r in ips], "visits":[dict(r) for r in visits]})


@app.route("/api/admin/reqs")
def admin_reqs():
    if not require_admin_check(): return jsonify({"error":"Нет доступа"}), 403
    c = db_conn()
    # Заявки = пользователи site_users у которых is_admin=0
    rows = c.execute("""
        SELECT su.id, su.nickname, su.is_banned, su.created_at,
               a.tg_name, a.phone, a.api_id
        FROM site_users su LEFT JOIN accounts a ON su.account_id=a.id
        WHERE su.is_admin=0
        ORDER BY su.id DESC
    """).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/test_notify", methods=["POST"])
def admin_test_notify():
    if not require_admin_check(): return jsonify({"error":"Нет доступа"}), 403
    chat_id = get_setting("notify_chat_id")
    if not chat_id: return jsonify({"error":"notify_chat_id не задан. Сначала зарегистрируйся как владелец."}), 400
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    tg_send(chat_id, f"✅ <b>Тест уведомлений BFG Platform</b>\n🕐 {ts}")
    return jsonify({"ok":True, "chat_id":chat_id})


# ══════════════════════════════════════════════════════
# TRADING
# ══════════════════════════════════════════════════════

_btc_history = []  # [(timestamp, price_str), ...]

@app.route("/api/trading/settings", methods=["GET","POST"])
def trading_settings():
    if request.method == "POST":
        d = request.json or {}
        c = db_conn()
        for k in ["trade_enabled","trade_buy_below","trade_sell_above","trade_amount","trade_interval"]:
            if k in d:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(d[k])))
        c.commit(); c.close()
        return jsonify({"ok":True})
    c = db_conn()
    rows = c.execute("SELECT key,value FROM settings WHERE key LIKE 'trade_%'").fetchall()
    c.close()
    return jsonify({r["key"]:r["value"] for r in rows})

@app.route("/api/trading/trades")
def get_trades():
    c = db_conn()
    rows = c.execute("""
        SELECT t.*, a.phone, a.tg_name FROM trades t
        LEFT JOIN accounts a ON t.account_id=a.id
        ORDER BY t.id DESC LIMIT 100
    """).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/trading/history")
def btc_history():
    return jsonify(_btc_history[-48:])

def _parse_btc_price(text):
    """Вытащить число из ответа бота типа 'Курс: 1 BTC = 95000 BFG'"""
    import re
    nums = re.findall(r"[\d\s]+", text.replace(",","."))
    for n in nums:
        n2 = n.replace(" ","")
        if n2.isdigit() and int(n2) > 100:
            return int(n2)
    return None

def trading_tick():
    """Проверить курс и выполнить сделку если нужно."""
    try:
        # Найти активный аккаунт
        c = db_conn()
        acc = c.execute("SELECT * FROM accounts WHERE is_active=1 AND session_string!='' LIMIT 1").fetchone()
        s = {r["key"]:r["value"] for r in c.execute("SELECT key,value FROM settings WHERE key LIKE 'trade_%'").fetchall()}
        c.close()
        if not acc: return
        if s.get("trade_enabled","0") != "1": return

        buy_below  = int(s.get("trade_buy_below","0")  or 0)
        sell_above = int(s.get("trade_sell_above","0") or 0)
        amount     = s.get("trade_amount","100")
        aid        = acc["id"]

        async def _check():
            cl = await get_client(aid)
            if not cl: return
            await cl.send_message(BOT, "Биткоин курс")
            import asyncio as _a; await _a.sleep(2)
            msgs = await cl.get_messages(BOT, limit=1)
            if not msgs: return
            text = msgs[0].text or ""
            price = _parse_btc_price(text)
            if not price: return
            ts = datetime.now().strftime("%H:%M")
            _btc_history.append({"ts": ts, "price": price})
            if len(_btc_history) > 48: _btc_history.pop(0)
            action = None
            if buy_below > 0 and price < buy_below:
                action = "buy"
                cmd = f"Биткоин купить {amount}"
            elif sell_above > 0 and price > sell_above:
                action = "sell"
                cmd = f"Биткоин продать {amount}"
            if action:
                await cl.send_message(BOT, cmd)
                import asyncio as _a; await _a.sleep(2)
                msgs2 = await cl.get_messages(BOT, limit=1)
                result = msgs2[0].text[:200] if msgs2 else ""
                c2 = db_conn()
                c2.execute("INSERT INTO trades(account_id,action,price,amount,result) VALUES(?,?,?,?,?)",
                           (aid, action, str(price), str(amount), result))
                c2.commit(); c2.close()
                print(f"[trade] {action} @ {price} amount={amount}")
        run_async(_check())
    except Exception as e:
        print(f"[trading_tick] {e}")

# ── COMMANDS DATA ────────────────────────────────────
COMMANDS_LIST = [
    {"s":"💡 Основные","cmds":[
        {"n":"Профиль","c":"Профиль"},{"n":"Мой лимит","c":"Мой лимит"},
        {"n":"Рейтинг","c":"Рейтинг"},{"n":"Топ","c":"Топ"},
        {"n":"Энергия","c":"Энергия"},{"n":"Опыт","c":"Опыт"},
        {"n":"Шахта","c":"Шахта"},{"n":"Копать","c":"Копать"},
        {"n":"Курс руды","c":"Курс руды"},{"n":"Ограбить мэрию","c":"Ограбить мэрию"},
        {"n":"Казна","c":"Казна"},{"n":"Ежедневный бонус","c":"Ежедневный бонус"},
        {"n":"Мой ник","c":"Мой ник"},{"n":"РП Команды","c":"РП Команды"},
        {"n":"Мой статус","c":"Мой статус"},{"n":"Статусы","c":"Статусы"},
        {"n":"!Беседа","c":"!Беседа"},
        {"n":"Сменить ник","c":"Сменить ник","p":"новый ник"},
    ]},
    {"s":"💰 Финансы","cmds":[
        {"n":"Б/Баланс","c":"Б/Баланс"},{"n":"Инвентарь","c":"Инвентарь"},
        {"n":"Банк положить","c":"Банк положить","p":"сумма"},
        {"n":"Банк снять","c":"Банк снять","p":"сумма"},
        {"n":"Депозит положить","c":"Депозит положить","p":"сумма"},
        {"n":"Депозит снять","c":"Депозит снять","p":"сумма"},
        {"n":"Дать деньги","c":"Дать","p":"сумма"},
        {"n":"Биткоин курс","c":"Биткоин курс"},{"n":"Биткоины","c":"Биткоины"},
        {"n":"Купить биткоин","c":"Биткоин купить","p":"кол-во"},
        {"n":"Продать биткоин","c":"Биткоин продать","p":"кол-во"},
        {"n":"Продать руду","c":"Продать","p":"кол-во"},
    ]},
    {"s":"🏠 Имущество","cmds":[
        {"n":"Машины","c":"Машины"},{"n":"Телефоны","c":"Телефоны"},
        {"n":"Самолёты","c":"Самолёты"},{"n":"Яхты","c":"Яхты"},
        {"n":"Вертолёты","c":"Вертолёты"},{"n":"Дома","c":"Дома"},
        {"n":"Мой дом","c":"Мой дом"},{"n":"Моя машина","c":"Моя машина"},
        {"n":"Мой телефон","c":"Мой телефон"},{"n":"Моя яхта","c":"Моя яхта"},
        {"n":"Мой самолёт","c":"Мой самолёт"},{"n":"Мой вертолёт","c":"Мой вертолёт"},
        {"n":"Мой чат","c":"Мой чат"},
    ]},
    {"s":"🏗 Постройки","cmds":[
        {"n":"Моя ферма","c":"Моя ферма"},{"n":"Построить ферму","c":"Построить ферму"},
        {"n":"Мой бизнес","c":"Мой бизнес"},{"n":"Построить бизнес","c":"Построить бизнес"},
        {"n":"Мой генератор","c":"Мой генератор"},{"n":"Построить генератор","c":"Построить генератор"},
        {"n":"Мой карьер","c":"Мой карьер"},{"n":"Построить карьер","c":"Построить карьер"},
        {"n":"Денежное дерево","c":"Денежное дерево"},{"n":"Моё дерево","c":"Моё дерево"},
        {"n":"Построить участок","c":"Построить участок"},
        {"n":"Мой сад","c":"Мой сад"},{"n":"Построить сад","c":"Построить сад"},
        {"n":"Сад полить","c":"Сад полить"},{"n":"Зелья","c":"Зелья"},
        {"n":"Создать зелье","c":"Создать зелье","p":"номер"},
    ]},
    {"s":"🎮 Игры","cmds":[
        {"n":"Испытать удачу","c":"Испытать удачу"},
        {"n":"Спин","c":"Спин","p":"ставка"},
        {"n":"Кубик","c":"Кубик","p2":["число 1-6","ставка"]},
        {"n":"Баскетбол","c":"Баскетбол","p":"ставка"},
        {"n":"Дартс","c":"Дартс","p":"ставка"},
        {"n":"Боулинг","c":"Боулинг","p":"ставка"},
        {"n":"Трейд вверх","c":"Трейд вверх","p":"ставка"},
        {"n":"Трейд вниз","c":"Трейд вниз","p":"ставка"},
        {"n":"Казино","c":"Казино","p":"ставка"},
        {"n":"Игра в слова","c":"Игра в слова"},
    ]},
    {"s":"🎭 Развлечения","cmds":[
        {"n":"Шар","c":"Шар","p":"фраза"},
        {"n":"Выбери","c":"Выбери","p":"вариант1 или вариант2"},
        {"n":"Инфа","c":"Инфа","p":"фраза"},
    ]},
    {"s":"💍 Браки & Кейсы","cmds":[
        {"n":"Мой брак","c":"Мой брак"},
        {"n":"Свадьба","c":"Свадьба","p":"ID"},
        {"n":"Развод","c":"Развод"},
        {"n":"Кейсы","c":"Кейсы"},
        {"n":"Купить кейс","c":"Купить кейс","p2":["номер","количество"]},
        {"n":"Открыть кейс","c":"Открыть кейс","p2":["номер","количество"]},
    ]},
    {"s":"⚔️ Кланы","cmds":[
        {"n":"Мой клан","c":"Мой клан"},{"n":"Клан топ","c":"Клан топ"},
        {"n":"Клан казна","c":"Клан казна"},{"n":"Клан выйти","c":"Клан выйти"},
        {"n":"Клан настройки","c":"Клан настройки"},
        {"n":"Клан пригласить","c":"Клан пригласить","p":"ID"},
        {"n":"Клан вступить","c":"Клан вступить","p":"ID клана"},
        {"n":"Клан исключить","c":"Клан исключить","p":"ID"},
        {"n":"Клан казна положить","c":"Клан казна","p":"сумма"},
        {"n":"Клан создать","c":"Клан создать","p":"название"},
        {"n":"Клан название","c":"Клан название","p":"новое"},
        {"n":"Клан повысить","c":"Клан повысить","p":"ID"},
        {"n":"Клан понизить","c":"Клан понизить","p":"ID"},
        {"n":"Клан передать","c":"Клан передать","p":"ID"},
        {"n":"Клан удалить","c":"Клан удалить"},
    ]},
]

# ═══════════════════════════════════════════════════════════════════════
# HTML — ВЕСЬ ФРОНТЕНД ВСТРОЕН
# ═══════════════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>BFG Platform</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
/* ── ТЕМЫ ── */
:root{
  --a1:#ff3333;--a2:#cc0000;--a3:#990000;
  --bg:#0d0000;--bg2:#1a0000;--bg3:#220000;--bg4:#2a0000;
  --bdr:#3d0000;--txt:#f0d0d0;--txt2:#c09090;--txt3:#805050;
  --grd:linear-gradient(135deg,#1a0000,#3a0000);
  --rad:8px;
}
body.theme-blue{--a1:#3399ff;--a2:#0066cc;--a3:#004499;--bg:#000d1a;--bg2:#001a33;--bg3:#002244;--bg4:#002a55;--bdr:#003d77;--txt:#d0e8ff;--txt2:#90b0d0;--txt3:#506070;--grd:linear-gradient(135deg,#001a33,#003a77)}
body.theme-green{--a1:#33ff88;--a2:#00cc55;--a3:#009933;--bg:#001a0d;--bg2:#003319;--bg3:#004422;--bg4:#00552a;--bdr:#006633;--txt:#d0f0e0;--txt2:#90c0a0;--txt3:#507060;--grd:linear-gradient(135deg,#001a0d,#003a19)}
body.theme-purple{--a1:#cc66ff;--a2:#9900cc;--a3:#660099;--bg:#0d001a;--bg2:#1a0033;--bg3:#220044;--bg4:#2a0055;--bdr:#3d0077;--txt:#ead0ff;--txt2:#b090d0;--txt3:#705080;--grd:linear-gradient(135deg,#1a0033,#3a0077)}

*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--txt);font-family:-apple-system,'Segoe UI',sans-serif;font-size:14px;line-height:1.4}
::-webkit-scrollbar{width:3px}::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:2px}
a{color:var(--a1);text-decoration:none}

/* ── LAYOUT ── */
#app{display:flex;flex-direction:column;height:100vh}
.content{flex:1;overflow-y:auto;padding-bottom:65px}
.page{padding:14px;display:none;animation:fi .25s ease}
.page.active{display:block}
@keyframes fi{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}

/* ── TOPBAR ── */
.topbar{background:var(--grd);border-bottom:1px solid var(--bdr);padding:11px 15px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50}
.logo{font-size:20px;font-weight:900;letter-spacing:4px;
  background:linear-gradient(135deg,var(--a1),#ffaaaa);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.acc-chip{font-size:11px;color:var(--a1);background:rgba(255,51,51,.1);
  border:1px solid rgba(255,51,51,.2);padding:4px 10px;border-radius:4px;
  cursor:pointer;max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  transition:background .2s}
.acc-chip:hover{background:rgba(255,51,51,.2)}

/* ── NAV ── */
.nav{position:fixed;bottom:0;left:0;right:0;background:var(--grd);border-top:1px solid var(--bdr);
  display:flex;height:58px;z-index:100}
.nb{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;
  font-size:10px;font-weight:600;color:var(--txt3);cursor:pointer;border:none;background:none;
  border-top:2px solid transparent;transition:all .15s}
.nb .ni{font-size:18px;transition:transform .2s}
.nb:hover{color:var(--a1)}.nb:hover .ni{transform:scale(1.15)}
.nb.on{color:var(--a1);border-top-color:var(--a1)}
.nb.on .ni{transform:scale(1.1)}

/* ── CARDS ── */
.card{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--rad);padding:14px;margin-bottom:10px;
  animation:fi .3s ease}
.sec-title{font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;
  color:var(--a1);margin-bottom:10px;display:flex;align-items:center;gap:8px}
.sec-title::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--bdr),transparent)}

/* ── FORM ── */
.lbl{display:block;font-size:11px;color:var(--txt3);margin-bottom:4px;font-weight:600}
.inp{width:100%;background:var(--bg3);border:1px solid var(--bdr);border-radius:5px;
  padding:9px 11px;color:var(--txt);font-size:14px;outline:none;transition:border-color .15s}
.inp:focus{border-color:var(--a2)}.inp::placeholder{color:var(--txt3)}
select.inp{cursor:pointer}
.fld{margin-bottom:11px}.row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px}

/* ── BUTTONS ── */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:5px;
  padding:9px 14px;border:none;border-radius:5px;cursor:pointer;
  font-size:13px;font-weight:700;transition:all .15s;user-select:none}
.btn:active{transform:scale(.95)}
.btn-red{background:linear-gradient(135deg,var(--a2),var(--a3));color:#fff;box-shadow:0 2px 8px rgba(0,0,0,.3)}
.btn-red:hover{transform:scale(1.04);box-shadow:0 4px 14px rgba(0,0,0,.4)}
.btn-drk{background:var(--bg3);border:1px solid var(--bdr);color:var(--txt2)}
.btn-drk:hover{border-color:var(--a2);color:var(--a1);transform:scale(1.02)}
.btn-ghost{background:rgba(255,51,51,.08);border:1px solid rgba(255,51,51,.2);color:var(--a1)}
.btn-ghost:hover{background:rgba(255,51,51,.18);transform:scale(1.02)}
.btn-sm{padding:5px 10px;font-size:11px}.btn-full{width:100%}
.btn:disabled{opacity:.3;cursor:not-allowed;transform:none!important}
.spin{display:inline-block;width:13px;height:13px;border:2px solid rgba(255,255,255,.25);
  border-top-color:#fff;border-radius:50%;animation:sp .6s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

/* ── AUTH ── */
.auth-box{background:var(--grd);border:1px solid var(--a3);border-radius:var(--rad);padding:18px}
.steps{display:flex;justify-content:center;gap:8px;margin-bottom:18px}
.st{width:26px;height:26px;border-radius:50%;border:2px solid var(--bdr);display:flex;align-items:center;
  justify-content:center;font-size:11px;font-weight:700;color:var(--txt3);transition:all .25s}
.st.on{border-color:var(--a1);color:var(--a1);background:rgba(255,51,51,.1)}
.st.dn{border-color:#00cc44;color:#00cc44;background:rgba(0,204,68,.1)}
.astep{display:none}.astep.active{display:block}
.err{background:rgba(255,51,51,.1);border:1px solid rgba(255,51,51,.25);border-radius:5px;
  padding:9px 12px;color:var(--a1);font-size:12px;margin-top:8px;display:none}
.suc{background:rgba(0,200,80,.1);border:1px solid rgba(0,200,80,.25);border-radius:5px;
  padding:9px 12px;color:#00cc44;font-size:12px}

/* ── ACCOUNTS ── */
.acc-row{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--rad);
  padding:11px 13px;margin-bottom:7px;display:flex;align-items:center;gap:10px;
  transition:border-color .2s}
.acc-row.ia{border-color:rgba(255,51,51,.3);box-shadow:0 0 10px rgba(180,0,0,.1)}
.acc-info{flex:1;min-width:0}
.acc-name{font-weight:700;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.acc-sub{font-size:11px;color:var(--txt3);margin-top:2px}

/* ── ACCORDION ── */
.acc-hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;
  cursor:pointer;user-select:none;background:var(--bg2);border:1px solid var(--bdr);
  border-radius:var(--rad);margin-bottom:6px;transition:all .2s}
.acc-hdr:hover{border-color:rgba(255,51,51,.3);background:var(--bg3)}
.acc-hdr.open{border-color:rgba(255,51,51,.3);border-bottom-left-radius:0;border-bottom-right-radius:0;margin-bottom:0}
.acc-hdr-txt{font-weight:700;font-size:13px;display:flex;align-items:center;gap:8px}
.acc-hdr-cnt{font-size:11px;color:var(--txt3);background:var(--bg3);padding:2px 7px;border-radius:10px}
.acc-arr{color:var(--txt3);transition:transform .25s;font-size:11px}
.acc-hdr.open .acc-arr{transform:rotate(180deg)}
.acc-body{display:none;background:var(--bg3);border:1px solid var(--bdr);
  border-top:none;border-radius:0 0 var(--rad) var(--rad);padding:10px;margin-bottom:6px}
.acc-body.open{display:block;animation:fi .2s ease}
.cmd-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}
@media(min-width:500px){.cmd-grid{grid-template-columns:repeat(3,1fr)}}
.cmd-card{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--rad);
  padding:10px;display:flex;flex-direction:column;gap:7px;
  transition:border-color .15s,transform .12s}
.cmd-card:hover{border-color:rgba(255,51,51,.25);transform:translateY(-1px)}
.cmd-name{font-size:12px;font-weight:700;line-height:1.3}
.cmd-inp{font-size:11px;padding:5px 8px}
.cmd-btns{display:flex;gap:5px}.cmd-btns .btn{flex:1;font-size:10px;padding:5px 3px}

/* ── RESPONSE ── */
.resp{background:var(--grd);border:1px solid var(--a3);border-radius:var(--rad);
  padding:13px;margin-bottom:14px;animation:fi .25s ease}
.resp-txt{font-size:13px;line-height:1.6;color:var(--txt2);white-space:pre-wrap;
  word-break:break-word;max-height:200px;overflow-y:auto}
.resp-btns{margin-top:9px;display:flex;flex-wrap:wrap;gap:6px}
.rbtn{padding:8px 13px;background:rgba(255,51,51,.1);border:1px solid rgba(255,51,51,.25);
  border-radius:5px;font-size:13px;color:var(--txt);cursor:pointer;font-weight:600;
  transition:all .15s}
.rbtn:hover{background:rgba(255,51,51,.22);border-color:var(--a1);transform:scale(1.03)}
.rbtn:active{transform:scale(.96)}

/* ── TASKS ── */
.task-item{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--rad);
  padding:12px;margin-bottom:8px;transition:border-color .2s}
.task-item.off{opacity:.5}
.task-hdr{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:7px}
.task-name{font-size:13px;font-weight:700}.task-meta{font-size:11px;color:var(--txt3);margin-top:2px}
.tags{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:8px}
.tag{background:var(--bg3);border:1px solid var(--bdr);padding:2px 7px;border-radius:3px;font-size:10px;color:var(--txt2)}
.tag-g{border-color:rgba(0,200,80,.3);color:#00cc44}
.tag-r{border-color:rgba(255,51,51,.3);color:var(--a1)}
.task-acts{display:flex;gap:5px;flex-wrap:wrap}

/* ── LOG ── */
.log-item{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--rad);
  padding:11px;margin-bottom:7px}
.log-hdr{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:5px;margin-bottom:5px}
.log-cmd{font-weight:700;font-size:13px}.log-ts{font-size:10px;color:var(--txt3)}
.log-res{font-size:12px;color:var(--txt2);line-height:1.5;max-height:80px;overflow:hidden;
  white-space:pre-wrap;word-break:break-word}
.log-btns{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px}
.lbtn{padding:3px 8px;background:rgba(255,51,51,.08);border:1px solid rgba(255,51,51,.2);
  border-radius:3px;font-size:11px;color:var(--a1);cursor:pointer}
.lbtn:hover{background:rgba(255,51,51,.18)}

/* ── STATS ── */
.stat-row{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:14px}
@media(min-width:500px){.stat-row{grid-template-columns:repeat(4,1fr)}}
.stat-box{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--rad);
  padding:12px;text-align:center}
.stat-val{font-size:26px;font-weight:900;color:var(--a1);line-height:1}
.stat-lbl{font-size:10px;color:var(--txt3);margin-top:3px;letter-spacing:1px;text-transform:uppercase}
.chart-wrap{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--rad);padding:14px;margin-bottom:10px;position:relative;height:220px}
.top-cmd-row{display:flex;align-items:center;justify-content:space-between;padding:7px 0;
  border-bottom:1px solid var(--bdr)}
.top-cmd-bar{height:4px;background:var(--a3);border-radius:2px;margin-top:4px}
.top-cmd-fill{height:100%;background:var(--a1);border-radius:2px;transition:width .5s ease}

/* ── SETTINGS ── */
.tog-row{display:flex;align-items:center;justify-content:space-between;padding:11px 0;border-bottom:1px solid var(--bdr)}
.tog-lbl{font-size:13px;font-weight:700}.tog-sub{font-size:11px;color:var(--txt3);margin-top:2px}
.tog{position:relative;width:44px;height:22px;flex-shrink:0}
.tog input{opacity:0;width:0;height:0}
.tog-tr{position:absolute;inset:0;background:#111;border:1px solid var(--bdr);border-radius:22px;cursor:pointer;transition:all .2s}
.tog-tr::before{content:'';position:absolute;width:14px;height:14px;border-radius:50%;background:#555;bottom:3px;left:3px;transition:all .2s}
.tog input:checked+.tog-tr{background:rgba(255,51,51,.15);border-color:var(--a1)}
.tog input:checked+.tog-tr::before{transform:translateX(22px);background:var(--a1)}
.spath{background:var(--bg3);border:1px solid var(--bdr);padding:8px 11px;border-radius:5px;
  font-size:11px;color:var(--txt3);font-family:monospace;word-break:break-all;margin-top:5px}
.srow{padding:11px 0;border-bottom:1px solid var(--bdr)}
.sl{font-size:13px;font-weight:700;margin-bottom:3px}.sd{font-size:11px;color:var(--txt3);margin-bottom:7px}
.theme-btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.theme-dot{width:28px;height:28px;border-radius:50%;cursor:pointer;border:3px solid transparent;
  transition:transform .2s,border-color .2s}
.theme-dot:hover{transform:scale(1.15)}
.theme-dot.sel{border-color:white;transform:scale(1.1)}

/* ── SESSION TABLE ── */
.ses-table{width:100%;border-collapse:collapse;font-size:12px}
.ses-table th{background:var(--bg3);padding:8px 10px;text-align:left;font-size:10px;letter-spacing:1px;color:var(--a1)}
.ses-table td{padding:7px 10px;border-bottom:1px solid var(--bdr);color:var(--txt2)}
.ses-table tr:hover td{background:var(--bg3)}

/* ── MODAL ── */
.mover{position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.85);
  display:flex;align-items:flex-end;justify-content:center;padding:16px}
.mover.h{display:none}
.modal{background:var(--bg2);border:1px solid var(--a3);border-radius:10px 10px 0 0;
  padding:20px;width:100%;max-width:480px;max-height:88vh;overflow-y:auto}
.modal-title{font-size:14px;font-weight:800;color:var(--a1);margin-bottom:14px}
.modal-x{float:right;background:none;border:none;color:var(--txt3);font-size:18px;cursor:pointer}
.modal-x:hover{color:var(--a1)}

/* ── TOASTS ── */
#toasts{position:fixed;bottom:68px;right:12px;z-index:999;display:flex;flex-direction:column;gap:5px;pointer-events:none}
.toast{border:1px solid;border-radius:5px;padding:9px 13px;font-size:12px;font-weight:700;max-width:260px;animation:fi .2s ease}
.toast-ok{background:rgba(0,200,80,.12);border-color:rgba(0,200,80,.3);color:#00cc44}
.toast-err{background:rgba(255,51,51,.12);border-color:rgba(255,51,51,.3);color:var(--a1)}
.toast-info{background:rgba(255,180,0,.12);border-color:rgba(255,180,0,.3);color:#ffc832}
.cached-badge{font-size:10px;color:#ffc832;margin-left:5px}

/* ── PROFILE CARDS ── */
.profiles-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.prof-card{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--rad);
  padding:14px;text-align:center;transition:all .2s}
.prof-card:hover{transform:translateY(-2px);border-color:rgba(255,51,51,.3)}
.prof-card.creator{border-color:var(--a1);box-shadow:0 0 18px rgba(255,51,51,.2)}
.prof-avatar{width:52px;height:52px;border-radius:50%;background:linear-gradient(135deg,var(--a3),var(--a1));
  display:flex;align-items:center;justify-content:center;font-size:22px;margin:0 auto 8px;border:2px solid var(--bdr)}
.prof-avatar.cr{border-color:var(--a1);background:linear-gradient(135deg,#990000,#ff3333)}
.prof-nick{font-size:12px;font-weight:700;color:var(--txt);margin-bottom:4px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.prof-badge{display:inline-block;padding:2px 7px;border-radius:3px;font-size:9px;font-weight:700;margin-bottom:5px}
.pb-creator{background:linear-gradient(135deg,var(--a2),var(--a1));color:#fff}
.pb-user{background:rgba(255,51,51,.1);border:1px solid var(--bdr);color:var(--txt3)}
.pb-ban{background:rgba(100,100,100,.2);border:1px solid #444;color:#888}
.prof-stats{font-size:10px;color:var(--txt3)}
/* ── AUTH TABS ── */
.auth-tabs{display:flex;gap:3px;margin-bottom:16px;background:rgba(0,0,0,.3);border-radius:5px;padding:3px}
.atab{flex:1;padding:8px;text-align:center;cursor:pointer;border-radius:4px;
  font-size:12px;font-weight:700;color:var(--txt3);transition:all .2s;border:none;background:none}
.atab.on{background:var(--a2);color:#fff}
/* ── ADMIN TABLE ── */
.adm-tbl{width:100%;border-collapse:collapse;font-size:12px}
.adm-tbl th{background:var(--bg3);padding:7px 10px;text-align:left;font-size:10px;
  letter-spacing:1px;color:var(--a1);text-transform:uppercase}
.adm-tbl td{padding:7px 10px;border-bottom:1px solid var(--bdr);color:var(--txt2)}
.adm-tbl tr:hover td{background:var(--bg3)}
/* ── TRADING ── */
.trade-row{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--bdr)}
.trade-buy{color:#00cc44}.trade-sell{color:var(--a1)}
.trade-hist{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--rad);padding:13px;height:200px;position:relative;margin-bottom:10px}
.trade-item{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--rad);padding:10px;margin-bottom:6px;font-size:12px}
</style>
</head>
<body>
<div id="app">

<header class="topbar">
  <div class="logo">BFG</div>
  <div id="acc-chip" class="acc-chip" style="display:none" onclick="go('accounts')">—</div>
</header>

<div class="content">

  <!-- ══ АККАУНТЫ ══ -->
  <div class="page active" id="pg-accounts">

    <!-- Блок входа/регистрации на сайт -->
    <div id="site-auth-block" class="card" style="margin-bottom:14px">
      <div class="auth-tabs">
        <button class="atab on" id="atab-login" onclick="switchAuthTab('login')">Войти</button>
        <button class="atab" id="atab-reg" onclick="switchAuthTab('reg')">Регистрация</button>
      </div>
      <!-- ВХОД -->
      <div id="site-login-form">
        <div class="fld"><label class="lbl">Никнейм</label>
          <input class="inp" id="sl-nick" placeholder="Твой никнейм" onkeydown="if(event.key==='Enter')siteLogin()"></div>
        <div class="fld"><label class="lbl">Пароль</label>
          <input class="inp" id="sl-pw" type="password" placeholder="Пароль" onkeydown="if(event.key==='Enter')siteLogin()"></div>
        <button class="btn btn-red btn-full" id="sl-btn" onclick="siteLogin()">🔑 Войти</button>
        <div class="err" id="sl-err"></div>
      </div>
      <!-- РЕГИСТРАЦИЯ -->
      <div id="site-reg-form" style="display:none">
        <div class="fld"><label class="lbl">Никнейм</label>
          <input class="inp" id="sr-nick" placeholder="Придумай никнейм"></div>
        <div class="fld"><label class="lbl">Пароль (мин. 4 символа)</label>
          <input class="inp" id="sr-pw" type="password" placeholder="Придумай пароль"></div>
        <div style="font-size:11px;color:var(--txt3);margin-bottom:8px">
          ⚠️ Сначала авторизуй Telegram-аккаунт ниже, потом нажми Зарегистрироваться
        </div>
        <button class="btn btn-red btn-full" id="sr-btn" onclick="siteRegister()">📝 Зарегистрироваться</button>
        <div class="err" id="sr-err"></div>
      </div>
      <!-- Статус: вошёл -->
      <div id="site-logged" style="display:none">
        <div class="suc" id="site-logged-msg"></div>
        <button class="btn btn-drk btn-sm" style="margin-top:8px" onclick="siteLogout()">Выйти из аккаунта</button>
      </div>
    </div>

    <div class="sec-title">🔐 Telegram Авторизация</div>
    <div class="auth-box">
      <div class="steps">
        <div class="st on" id="s1">1</div>
        <div class="st" id="s2">2</div>
        <div class="st" id="s3">3</div>
        <div class="st" id="s4">4</div>
      </div>
      <div class="astep active" id="a1">
        <div class="fld"><label class="lbl">API ID</label><input class="inp" id="f-id" type="number" placeholder="12345678"></div>
        <div class="fld"><label class="lbl">API Hash</label><input class="inp" id="f-hash" placeholder="0f1a2b3c..."></div>
        <div class="fld"><label class="lbl">Телефон</label><input class="inp" id="f-phone" placeholder="+79991234567"></div>
        <div style="font-size:11px;color:var(--txt3);margin-bottom:11px">Ключи: <b style="color:var(--a1)">my.telegram.org</b></div>
        <button class="btn btn-red btn-full" id="btn-code" onclick="sendCode()">📱 Отправить код</button>
        <div class="err" id="e1"></div>
      </div>
      <div class="astep" id="a2">
        <div style="font-size:13px;color:var(--txt2);margin-bottom:12px">Код отправлен в <b style="color:var(--a1)">Telegram</b></div>
        <div class="fld"><label class="lbl">Код из Telegram</label>
          <input class="inp" id="f-code" placeholder="12345" maxlength="6" oninput="if(this.value.length>=5)verifyCode()"></div>
        <div class="fld"><label class="lbl">Пароль 2FA (если есть)</label>
          <input class="inp" id="f-2fa" type="password" placeholder="Оставь пустым если нет"></div>
        <button class="btn btn-red btn-full" id="btn-verify" onclick="verifyCode()">✓ Подтвердить</button>
        <div class="err" id="e2"></div>
      </div>
      <div class="astep" id="a3">
        <div style="font-size:13px;color:var(--txt2);margin-bottom:12px">На аккаунте включена <b style="color:var(--a1)">двухфакторная защита</b>. Введи облачный пароль:</div>
        <div class="fld"><label class="lbl">Пароль 2FA</label>
          <input class="inp" id="f-2fa-only" type="password" placeholder="Твой пароль Telegram" onkeydown="if(event.key==='Enter')verify2FA()"></div>
        <button class="btn btn-red btn-full" id="btn-2fa" onclick="verify2FA()">🔓 Войти</button>
        <div class="err" id="e3"></div>
      </div>
      <div class="astep" id="a4">
        <div class="suc" id="auth-ok"></div>
        <button class="btn btn-red btn-full" style="margin-top:11px" onclick="go('commands')">🚀 К командам</button>
      </div>
    </div>
    <div style="margin-top:14px">
      <div class="sec-title">👤 Аккаунты</div>
      <div id="accs-list"></div>
    </div>
  </div>

  <!-- ══ КОМАНДЫ ══ -->
  <div class="page" id="pg-commands">
    <div id="bot-resp" style="display:none"></div>
    <div id="cmds-cont"></div>
  </div>

  <!-- ══ ЗАДАЧИ ══ -->
  <div class="page" id="pg-tasks">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
      <div style="font-size:15px;font-weight:800;color:var(--a1)">⏱ Задачи</div>
      <button class="btn btn-red btn-sm" onclick="openTaskModal()">➕ Создать</button>
    </div>
    <div id="tasks-list"></div>
  </div>

  <!-- ══ ЛОГ ══ -->
  <div class="page" id="pg-log">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
      <div style="font-size:15px;font-weight:800;color:var(--a1)">📜 Лог</div>
      <button class="btn btn-drk btn-sm" onclick="clearLog()">🗑 Очистить</button>
    </div>
    <div id="log-list"></div>
  </div>

  <!-- ══ НАСТРОЙКИ ══ -->
  <div class="page" id="pg-settings">
    <div style="font-size:15px;font-weight:800;color:var(--a1);margin-bottom:14px">⚙️ Настройки</div>
    <div class="card">

      <div class="tog-row">
        <div><div class="tog-lbl">🤖 Автовыполнение</div><div class="tog-sub">Задачи по расписанию</div></div>
        <label class="tog"><input type="checkbox" id="t-auto" onchange="sv('auto_enabled',this.checked?'1':'0')"><span class="tog-tr"></span></label>
      </div>

      <div class="tog-row">
        <div><div class="tog-lbl">🔔 Уведомления о командах</div><div class="tog-sub">Отправлять в Telegram</div></div>
        <label class="tog"><input type="checkbox" id="t-ncmd" onchange="sv('notify_commands',this.checked?'1':'0')"><span class="tog-tr"></span></label>
      </div>

      <div class="tog-row">
        <div><div class="tog-lbl">📝 Уведомления о регистрации</div><div class="tog-sub">Одобрение новых аккаунтов</div></div>
        <label class="tog"><input type="checkbox" id="t-nreg" onchange="sv('notify_registrations',this.checked?'1':'0')"><span class="tog-tr"></span></label>
      </div>

      <div class="srow">
        <div class="sl">Telegram Chat ID</div>
        <div class="sd">Куда слать уведомления (твой ID или ID группы)</div>
        <div style="display:flex;gap:8px">
          <input class="inp" id="t-chatid" placeholder="123456789">
          <button class="btn btn-red btn-sm" onclick="saveNotifyChat()">Сохранить</button>
        </div>
      </div>

      <div class="srow">
        <div class="sl">Интервал проверки</div>
        <div class="sd">Секунды между проверкой задач</div>
        <div style="display:flex;gap:8px">
          <input class="inp" id="t-interval" type="number" min="10" style="max-width:90px" placeholder="60">
          <button class="btn btn-red btn-sm" onclick="saveInterval()">Сохранить</button>
        </div>
      </div>

      <div class="srow">
        <div class="sl">🎨 Цветовая тема</div>
        <div class="sd">Выбери цвет интерфейса</div>
        <div class="theme-btns">
          <div class="theme-dot sel" id="th-red" style="background:linear-gradient(135deg,#cc0000,#ff3333)" onclick="setTheme('red')" title="Красный"></div>
          <div class="theme-dot" id="th-blue" style="background:linear-gradient(135deg,#0066cc,#3399ff)" onclick="setTheme('blue')" title="Синий"></div>
          <div class="theme-dot" id="th-green" style="background:linear-gradient(135deg,#009933,#33ff88)" onclick="setTheme('green')" title="Зелёный"></div>
          <div class="theme-dot" id="th-purple" style="background:linear-gradient(135deg,#9900cc,#cc66ff)" onclick="setTheme('purple')" title="Фиолетовый"></div>
        </div>
      </div>

      <div class="srow" style="border:none">
        <div class="sl">📁 База данных</div>
        <div class="spath" id="db-path">—</div>
      </div>

      <div style="padding-top:12px;text-align:center">
        <a href="https://t.me/TgReason" target="_blank" style="font-size:14px;font-weight:700">💬 Поддержка: t.me/TgReason</a>
      </div>
    </div>
  </div>


  <!-- ══ ADMIN ══ -->
  <div class="page" id="pg-admin">
    <div style="font-size:15px;font-weight:800;color:var(--a1);margin-bottom:14px">🛡 Администрация</div>
    <div class="card" style="margin-bottom:10px">
      <div class="sec-title">🚫 Бан по номеру</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
        <input class="inp" id="ban-phone" placeholder="+79991234567" style="flex:1;min-width:140px">
        <input class="inp" id="ban-phone-reason" placeholder="Причина" style="flex:1;min-width:100px">
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-red btn-sm" onclick="adminBanPhone()">🚫 Забанить</button>
        <button class="btn btn-drk btn-sm" onclick="adminUnbanPhone()">✅ Разбанить</button>
      </div>
    </div>
    <div class="card" style="margin-bottom:10px">
      <div class="sec-title">🌐 Бан по IP</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
        <input class="inp" id="ban-ip" placeholder="192.168.1.1" style="flex:1;min-width:140px">
        <input class="inp" id="ban-ip-reason" placeholder="Причина" style="flex:1;min-width:100px">
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-red btn-sm" onclick="adminBanIp()">🚫 Забанить IP</button>
        <button class="btn btn-drk btn-sm" onclick="adminUnbanIp()">✅ Разбанить IP</button>
      </div>
    </div>
    <div class="card" style="margin-bottom:10px">
      <div class="sec-title">🗑 Удалить пользователя</div>
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <input class="inp" id="del-uid" type="number" placeholder="ID пользователя" style="flex:1">
        <button class="btn btn-red btn-sm" onclick="adminDelUser()">Удалить</button>
      </div>
    </div>
    <div class="card">
      <div class="sec-title">📬 Заявки на регистрацию</div>
      <div id="reqs-empty" style="font-size:12px;color:var(--txt3);padding:8px 0">Загрузка...</div>
      <div id="reqs-list"></div>
    </div>
    <div class="card" style="margin-top:10px">
      <div class="sec-title">📋 Заблокированные</div>
      <div id="banned-list" style="font-size:12px;color:var(--txt3)">Нажми загрузить</div>
      <button class="btn btn-drk btn-sm" style="margin-top:8px" onclick="loadBanned()">Загрузить</button>
    </div>
    <div style="margin-top:12px">
      <button class="btn btn-red btn-sm" onclick="testNotify()">🔔 Тест уведомления</button>
    </div>
  </div>


  <!-- ══ ТРЕЙДИНГ ══ -->
  <div class="page" id="pg-trading">
    <div style="font-size:15px;font-weight:800;color:var(--a1);margin-bottom:14px">📈 Авто-трейдинг BTC</div>

    <div class="card" style="margin-bottom:10px">
      <div class="sec-title">⚙️ Настройки стратегии</div>
      <div class="trade-row">
        <div><div class="tog-lbl">🤖 Автотрейдинг</div><div class="tog-sub">Покупать/продавать автоматически</div></div>
        <label class="tog"><input type="checkbox" id="tr-enabled" onchange="saveTradingSettings()"><span class="tog-tr"></span></label>
      </div>
      <div style="margin-top:12px" class="row2">
        <div class="fld">
          <label class="lbl">Покупать при курсе ниже</label>
          <input class="inp" id="tr-buy" type="number" placeholder="0 = выключено">
        </div>
        <div class="fld">
          <label class="lbl">Продавать при курсе выше</label>
          <input class="inp" id="tr-sell" type="number" placeholder="0 = выключено">
        </div>
      </div>
      <div class="row2">
        <div class="fld">
          <label class="lbl">Сумма сделки (BFG)</label>
          <input class="inp" id="tr-amount" type="number" placeholder="100">
        </div>
        <div class="fld">
          <label class="lbl">Интервал проверки (мин)</label>
          <input class="inp" id="tr-interval" type="number" min="1" placeholder="5">
        </div>
      </div>
      <button class="btn btn-red btn-full" onclick="saveTradingSettings()">💾 Сохранить настройки</button>
    </div>

    <div class="card" style="margin-bottom:10px">
      <div class="sec-title">📊 Курс BTC (последние точки)</div>
      <div class="trade-hist"><canvas id="chart-btc"></canvas></div>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div style="font-size:12px;color:var(--txt3)">Текущий курс: <span id="btc-cur" style="color:var(--a1);font-weight:700">—</span></div>
        <button class="btn btn-drk btn-sm" onclick="checkBtcNow()">🔄 Проверить сейчас</button>
      </div>
    </div>

    <div class="card">
      <div class="sec-title">📋 История сделок</div>
      <div id="trades-list"><div style="text-align:center;padding:20px;color:var(--txt3)">Нет сделок</div></div>
    </div>
  </div>

</div><!-- /content -->

<nav class="nav">
  <button class="nb on" data-p="accounts" onclick="go('accounts')"><span class="ni">👤</span>Аккаунты</button>
  <button class="nb" data-p="commands" onclick="go('commands')"><span class="ni">💬</span>Команды</button>
  <button class="nb" data-p="tasks" onclick="go('tasks')"><span class="ni">⏱</span>Задачи</button>
  <button class="nb" data-p="log" onclick="go('log')"><span class="ni">📜</span>Лог</button>
  <button class="nb" data-p="settings" onclick="go('settings')"><span class="ni">⚙️</span>Настройки</button>
  <button class="nb" data-p="trading" onclick="go('trading')"><span class="ni">📈</span>Трейдинг</button>
  <button class="nb" id="nb-admin" data-p="admin" onclick="go('admin')" style="display:none"><span class="ni">🛡</span>Админ</button>
</nav>
</div>

<!-- TASK MODAL -->
<div class="mover h" id="task-modal">
  <div class="modal">
    <button class="modal-x" onclick="cm()">✕</button>
    <div class="modal-title">➕ Новая задача</div>
    <div class="fld"><label class="lbl">Аккаунт</label><select class="inp" id="t-acc"></select></div>
    <div class="fld"><label class="lbl">Команда</label><select class="inp" id="t-cmd" onchange="onCC()"></select></div>
    <div class="fld" id="t-pw" style="display:none"><label class="lbl" id="t-pl">Параметр</label>
      <input class="inp" id="t-param" placeholder=""></div>
    <div class="fld"><label class="lbl">Кнопка (необязательно)</label>
      <input class="inp" id="t-btn" placeholder="Собрать"></div>
    <div class="row3">
      <div class="fld"><label class="lbl">Каждые</label><input class="inp" id="t-delay" type="number" min="1" value="2"></div>
      <div class="fld"><label class="lbl">Ед.</label>
        <select class="inp" id="t-unit"><option value="minutes">мин</option><option value="hours" selected>ч</option><option value="days">дн</option></select></div>
      <div class="fld"><label class="lbl">Повтор</label>
        <select class="inp" id="t-repeat" onchange="onRC()">
          <option value="infinite">∞ Всегда</option><option value="once">1 раз</option>
          <option value="daily">Ежедневно</option><option value="n_times">N раз</option>
        </select></div>
    </div>
    <div class="fld" id="t-nw" style="display:none"><label class="lbl">Раз</label>
      <input class="inp" id="t-n" type="number" min="1" value="5"></div>
    <button class="btn btn-red btn-full" onclick="saveTask()">💾 Сохранить</button>
  </div>
</div>

<div id="toasts"></div>

<script>
// ═══ STATE ═══════════════════════════════════════════
let AID = null, authPhone = null, allCmds = [], charts = {}, pollT = null;

// ═══ TELEGRAM WEB APP ═══════════════════════════════
(function(){
  try {
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg) {
      tg.expand();
      tg.ready();
      // Применить тему Telegram
      if (tg.colorScheme === 'light') {
        document.documentElement.style.setProperty('--bg','#f5f5f5');
        document.documentElement.style.setProperty('--txt','#111');
      }
    }
  } catch(e) { console.log('TG WebApp:', e); }
})();

// ═══ INIT ════════════════════════════════════════════
window.onload = async () => {
  // Проверяем сессию на сервере (Flask session через cookie)
  const me = await api('/api/site/me');
  if (me && !me.error) {
    // Сессия жива — восстанавливаем всё
    SITE_USER = me;
    localStorage.setItem('bfg_user', JSON.stringify(me));
    // Восстановить активный аккаунт
    if (me.account_id) {
      AID = me.account_id;
    }
    onSiteLogin();
  } else {
    // Сессия мертва — чистим localStorage
    localStorage.removeItem('bfg_user');
    SITE_USER = null;
  }
  await loadAccounts();
  await loadCmds();
  await loadSettings();
  buildCmdsUI();
  pollT = setInterval(async () => {
    const pg = document.querySelector('.page.active')?.id;
    if (pg === 'pg-tasks') loadTasks();
    if (pg === 'pg-log')   loadLog();
  }, 10000);
};

// ═══ API ═════════════════════════════════════════════
async function api(url, method='GET', body=null) {
  const o = {method, headers:{'Content-Type':'application/json'}};
  if (body) o.body = JSON.stringify(body);
  try { const r = await fetch(url, o); return await r.json(); }
  catch(e) { return {error:'Сервер: '+e.message}; }
}

// ═══ NAV ═════════════════════════════════════════════
function go(p) {
  document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nb').forEach(el => el.classList.toggle('on', el.dataset.p === p));
  document.getElementById('pg-'+p)?.classList.add('active');
  if (p==='tasks')    loadTasks();
  if (p==='log')      loadLog();
  if (p==='accounts') loadAccounts();
  if (p==='settings') loadSettings();
}

// ═══ AUTH ════════════════════════════════════════════
function setStep(n) {
  [1,2,3,4].forEach(i => {
    const a = document.getElementById('a'+i);
    if (a) a.classList.toggle('active', i===n);
    const s = document.getElementById('s'+i);
    if (s) s.className = 'st'+(i<n?' dn':i===n?' on':'');
  });
}
function showErr(id, msg) { const e = document.getElementById(id); e.textContent='❌ '+msg; e.style.display='block'; }
function hideErr(id) { document.getElementById(id).style.display='none'; }

async function sendCode() {
  hideErr('e1');
  const btn = document.getElementById('btn-code');
  btn.disabled=true; btn.innerHTML='<span class="spin"></span> Отправка...';
  const r = await api('/api/auth/start','POST',{
    api_id: document.getElementById('f-id').value.trim(),
    api_hash: document.getElementById('f-hash').value.trim(),
    phone: document.getElementById('f-phone').value.trim(),
  });
  btn.disabled=false; btn.textContent='📱 Отправить код';
  if (r.error) { showErr('e1',r.error); return; }
  authPhone = document.getElementById('f-phone').value.trim();
  if (r.state==='done') { AID=r.account_id; authDone(r.name); return; }
  toast('Код отправлен!','ok'); setStep(2);
}

async function verifyCode() {
  hideErr('e2');
  const btn = document.getElementById('btn-verify');
  btn.disabled=true; btn.innerHTML='<span class="spin"></span> Проверка...';
  const r = await api('/api/auth/verify','POST',{
    phone: authPhone,
    code: document.getElementById('f-code').value.trim(),
    password: document.getElementById('f-2fa').value,
  });
  btn.disabled=false; btn.textContent='✓ Подтвердить';
  if (r.error) { showErr('e2',r.error); return; }
  if (r.state==='need_2fa') {
    setStep(3);
    toast('Введи пароль 2FA ниже', 'info');
    setTimeout(() => document.getElementById('f-2fa-only')?.focus(), 100);
    return;
  }
  AID = r.account_id; authDone(r.name);
}

async function verify2FA() {
  const e3 = document.getElementById('e3');
  e3.style.display='none';
  const btn = document.getElementById('btn-2fa');
  btn.disabled=true; btn.innerHTML='<span class="spin"></span> Вхожу...';
  const pwd = document.getElementById('f-2fa-only').value;
  if (!pwd) { e3.textContent='❌ Введи пароль'; e3.style.display='block'; btn.disabled=false; btn.textContent='🔓 Войти'; return; }
  const r = await api('/api/auth/verify','POST',{phone: authPhone, code:'', password: pwd});
  btn.disabled=false; btn.textContent='🔓 Войти';
  if (r.error) { e3.textContent='❌ '+r.error; e3.style.display='block'; return; }
  AID = r.account_id; authDone(r.name);
}

function authDone(name) {
  setStep(4);
  document.getElementById('auth-ok').textContent='✅ Вошёл как: '+(name||'пользователь');
  updateChip(name); toast('Авторизация успешна!','ok'); loadAccounts();
}

// ═══ ACCOUNTS ════════════════════════════════════════
async function loadAccounts() {
  const data = await api('/api/accounts');
  if (!data||data.error) return;
  const active = data.find(a=>a.is_active);
  if (active && !AID) { AID=active.id; updateChip(active.tg_name||active.phone); }
  const el = document.getElementById('accs-list');
  if (!data.length) { el.innerHTML='<div style="text-align:center;padding:30px;color:var(--txt3)">Нет аккаунтов</div>'; return; }
  el.innerHTML = data.map(a=>`
    <div class="acc-row ${a.is_active?'ia':''}">
      <div class="acc-info">
        <div class="acc-name">${a.is_active?'⭐ ':''}${esc(a.tg_name||a.phone)}</div>
        <div class="acc-sub">${a.phone} · <span style="color:${a.session_string?'#00cc44':'var(--a1)'}">
          ${a.session_string?'✓ Авторизован':'Не авторизован'}</span></div>
      </div>
      <div style="display:flex;gap:5px">
        <button class="btn btn-ghost btn-sm" onclick="setActive(${a.id},'${esc(a.tg_name||a.phone)}')">⭐</button>
        <button class="btn btn-drk btn-sm" onclick="delAcc(${a.id})">🗑</button>
      </div>
    </div>`).join('');
}

async function setActive(id, name) {
  await api('/api/accounts/active','POST',{id}); AID=id; updateChip(name);
  toast('Аккаунт выбран!','ok'); loadAccounts();
}
async function delAcc(id) {
  if (!confirm('Удалить?')) return;
  await api('/api/accounts/'+id,'DELETE');
  if (AID===id){AID=null;document.getElementById('acc-chip').style.display='none';}
  toast('Удалено','info'); loadAccounts();
}
function updateChip(name) {
  const el=document.getElementById('acc-chip');
  el.textContent='👤 '+(name||'Аккаунт'); el.style.display='';
}

// ═══ COMMANDS (ACCORDION) ════════════════════════════
async function loadCmds() {
  const data = await api('/api/commands');
  if (Array.isArray(data)) allCmds = data;
}

function buildCmdsUI() {
  const cont = document.getElementById('cmds-cont');
  if (!allCmds.length) { cont.innerHTML='<div style="text-align:center;padding:30px;color:var(--txt3)">Загрузка...</div>'; return; }
  let html = '';
  allCmds.forEach((sec, si) => {
    const bid = 'sec'+si;
    html += `
    <div>
      <div class="acc-hdr" id="hdr${bid}" onclick="toggleAcc('${bid}')">
        <span class="acc-hdr-txt">${esc(sec.s)} <span class="acc-hdr-cnt">${sec.cmds.length}</span></span>
        <span class="acc-arr">▼</span>
      </div>
      <div class="acc-body" id="body${bid}">
        <div class="cmd-grid">`;
    sec.cmds.forEach((cmd, ci) => {
      const uid = `c${si}_${ci}`;
      let inp = '';
      if (cmd.p2) {
        inp = `<div style="display:flex;gap:3px">
          <input class="inp cmd-inp" id="${uid}_1" placeholder="${esc(cmd.p2[0])}">
          <input class="inp cmd-inp" id="${uid}_2" placeholder="${esc(cmd.p2[1])}">
        </div>`;
      } else if (cmd.p) {
        inp = `<input class="inp cmd-inp" id="${uid}_p" placeholder="${esc(cmd.p)}">`;
      }
      html += `<div class="cmd-card">
        <div class="cmd-name">${esc(cmd.n)}</div>
        ${inp}
        <div class="cmd-btns">
          <button class="btn btn-red" onclick="runCmd('${uid}','${escQ(cmd.c)}',${!!cmd.p},${!!cmd.p2})">▶</button>
          <button class="btn btn-drk" onclick="openTaskModal('${escQ(cmd.c)}','${escQ(cmd.n)}')">＋</button>
        </div>
      </div>`;
    });
    html += `</div></div></div>`;
  });
  cont.innerHTML = html;
  // Открыть первую секцию
  toggleAcc('sec0');
}

function toggleAcc(bid) {
  const hdr = document.getElementById('hdr'+bid);
  const body = document.getElementById('body'+bid);
  const isOpen = body.classList.contains('open');
  hdr.classList.toggle('open', !isOpen);
  body.classList.toggle('open', !isOpen);
}

async function runCmd(uid, baseCmd, hasP, hasP2) {
  if (!AID) { toast('Выбери аккаунт!','err'); go('accounts'); return; }
  let cmd = baseCmd;
  if (hasP2) {
    const p1 = document.getElementById(uid+'_1')?.value.trim()||'';
    const p2 = document.getElementById(uid+'_2')?.value.trim()||'';
    if (p1) cmd+=' '+p1; if (p2) cmd+=' '+p2;
  } else if (hasP) {
    const p = document.getElementById(uid+'_p')?.value.trim()||'';
    if (p) cmd+=' '+p;
  }
  toast('→ '+cmd,'info');
  const r = await api('/api/command/run','POST',{account_id:AID,command_text:cmd});
  if (r.error) { toast('❌ '+r.error,'err'); return; }
  showResp(r.text, r.buttons, r.cached);
  toast(r.cached ? '⚡ Из кэша' : '✓ Ответ получен!', r.cached?'info':'ok');
}

function showResp(text, btns, cached) {
  const box = document.getElementById('bot-resp');
  box.style.display='block';
  box.innerHTML = `<div class="resp">
    <div style="font-size:10px;color:var(--txt3);margin-bottom:5px">💬 Ответ бота${cached?'<span class="cached-badge">⚡ кэш</span>':''}</div>
    <div class="resp-txt">${esc(text||'(нет текста)')}</div>
    ${(btns||[]).length?'<div class="resp-btns">'+btns.map(b=>`<button class="rbtn" onclick="clickBtn('${escQ(b)}')">${esc(b)}</button>`).join('')+'</div>':''}
  </div>`;
  box.scrollIntoView({behavior:'smooth',block:'nearest'});
}

async function clickBtn(kw) {
  if (!AID) return;
  toast('🖱 '+kw,'info');
  const r = await api('/api/command/click','POST',{account_id:AID,keyword:kw});
  if (r.error) { toast('❌ '+r.error,'err'); return; }
  showResp(r.text, r.buttons);
}

// ═══ TASKS ═══════════════════════════════════════════
async function loadTasks() {
  const data = await api('/api/tasks');
  if (!data||data.error) return;
  const el = document.getElementById('tasks-list');
  if (!data.length) { el.innerHTML='<div style="text-align:center;padding:30px;color:var(--txt3)">Нет задач</div>'; return; }
  const rl={infinite:'∞',once:'1 раз',daily:'Ежедн.',n_times:'N раз'};
  const ul={minutes:'мин',hours:'ч',days:'дн'};
  el.innerHTML = data.map(t=>`
    <div class="task-item ${t.is_active?'':'off'}">
      <div class="task-hdr">
        <div><div class="task-name">${esc(t.command_text)}</div>
          <div class="task-meta">${esc(t.tg_name||t.phone||'—')}</div></div>
        <span class="tag ${t.is_active?'tag-g':'tag-r'}">${t.is_active?'✓':'✗'}</span>
      </div>
      <div class="tags">
        <span class="tag">Каждые ${t.delay_value} ${ul[t.delay_unit]||''}</span>
        <span class="tag">${rl[t.repeat_type]||t.repeat_type}</span>
        <span class="tag">×${t.run_count}</span>
        ${t.last_run?`<span class="tag">${t.last_run.substring(11,16)}</span>`:''}
      </div>
      <div class="task-acts">
        <button class="btn btn-ghost btn-sm" onclick="runNow(${t.id})">▶</button>
        <button class="btn btn-drk btn-sm" onclick="toggleT(${t.id})">${t.is_active?'⏸':'▶'}</button>
        <button class="btn btn-drk btn-sm" onclick="delT(${t.id})">🗑</button>
      </div>
    </div>`).join('');
}

async function openTaskModal(prefCmd='', prefName='') {
  const accs = await api('/api/accounts');
  const sel = document.getElementById('t-acc');
  sel.innerHTML = (accs||[]).filter(a=>a.session_string).map(a=>
    `<option value="${a.id}" ${a.id===AID?'selected':''}>${esc(a.tg_name||a.phone)}</option>`
  ).join('');
  if (!sel.options.length) { toast('Нет аккаунтов!','err'); return; }
  const cs = document.getElementById('t-cmd'); let opts='';
  allCmds.forEach(sec => sec.cmds.forEach(c=>{
    opts+=`<option value="${escAttr(c.c)}" data-p="${escAttr(c.p||'')}"${c.c===prefCmd?' selected':''}>${esc(c.n)}</option>`;
  }));
  cs.innerHTML = opts; onCC();
  document.getElementById('t-nw').style.display='none';
  document.getElementById('task-modal').classList.remove('h');
}

function cm() { document.getElementById('task-modal').classList.add('h'); }
function onCC() {
  const sel=document.getElementById('t-cmd'), p=sel.options[sel.selectedIndex]?.getAttribute('data-p')||'';
  const w=document.getElementById('t-pw');
  if(p){w.style.display='block';document.getElementById('t-pl').textContent='Параметр: '+p;document.getElementById('t-param').placeholder=p;}
  else w.style.display='none';
}
function onRC() { document.getElementById('t-nw').style.display=document.getElementById('t-repeat').value==='n_times'?'block':'none'; }

async function saveTask() {
  const accId=document.getElementById('t-acc').value;
  let cmd=document.getElementById('t-cmd').value.trim();
  const param=document.getElementById('t-param').value.trim(); if(param) cmd+=' '+param;
  if(!accId||!cmd){toast('Заполни!','err');return;}
  const r=await api('/api/tasks','POST',{account_id:parseInt(accId),command_text:cmd,btn_keyword:document.getElementById('t-btn').value.trim(),delay_value:parseInt(document.getElementById('t-delay').value)||2,delay_unit:document.getElementById('t-unit').value,repeat_type:document.getElementById('t-repeat').value,repeat_n:parseInt(document.getElementById('t-n').value)||1});
  if(r.error){toast('❌ '+r.error,'err');return;}
  cm();toast('Задача создана!','ok');loadTasks();
}

async function toggleT(id){await api('/api/tasks/'+id+'/toggle','POST');loadTasks();}
async function delT(id){if(!confirm('Удалить?'))return;await api('/api/tasks/'+id,'DELETE');toast('Удалено','info');loadTasks();}
async function runNow(id){await api('/api/tasks/'+id+'/run','POST');toast('Запущено!','ok');setTimeout(loadLog,5000);setTimeout(loadTasks,6000);}

// ═══ LOG ═════════════════════════════════════════════
async function loadLog() {
  const data = await api('/api/logs');
  if (!data||data.error) return;
  const el = document.getElementById('log-list');
  if (!data.length){el.innerHTML='<div style="text-align:center;padding:30px;color:var(--txt3)">Лог пуст</div>';return;}
  el.innerHTML = data.map(l=>{
    let btns='';
    try{const b=JSON.parse(l.buttons||'[]');if(b.length)btns='<div class="log-btns">'+b.map(x=>`<span class="lbtn" onclick="clickBtn('${escQ(x)}')">${esc(x)}</span>`).join('')+'</div>';}catch{}
    return `<div class="log-item">
      <div class="log-hdr">
        <div style="display:flex;align-items:center;gap:6px">
          <span style="color:${l.status==='error'?'var(--a1)':'#00cc44'}">${l.status==='error'?'✗':'✓'}</span>
          <span class="log-cmd">${esc(l.command)}</span>
        </div>
        <span class="log-ts">${l.timestamp||''} · ${esc(l.tg_name||l.phone||'')}</span>
      </div>
      ${l.result?`<div class="log-res">${esc(l.result.substring(0,400))}</div>`:''}
      ${btns}
    </div>`;
  }).join('');
}

async function clearLog(){if(!confirm('Очистить?'))return;await api('/api/logs/clear','POST');toast('Очищено','info');loadLog();}

// ═══ STATISTICS ══════════════════════════════════════
let chartDaily=null;

async function loadStats() {
  const d = await api('/api/stats/daily');
  if (!d||d.error) return;

  // Stat boxes
  document.getElementById('stat-boxes').innerHTML = `
    <div class="stat-box"><div class="stat-val">${d.total}</div><div class="stat-lbl">Команд всего</div></div>
    <div class="stat-box"><div class="stat-val" style="color:#00cc44">${d.ok}</div><div class="stat-lbl">Успешных</div></div>
    <div class="stat-box"><div class="stat-val" style="color:var(--a1)">${d.errors}</div><div class="stat-lbl">Ошибок</div></div>
    <div class="stat-box"><div class="stat-val">${d.sessions}</div><div class="stat-lbl">Сессий</div></div>`;

  // Chart
  const daily = [...d.daily].reverse();
  const labels = daily.map(r=>r.date.substring(5));
  const ok_vals = daily.map(r=>r.successful_commands);
  const err_vals = daily.map(r=>r.failed_commands);

  if (chartDaily) chartDaily.destroy();
  const ctx = document.getElementById('chart-daily');
  if (ctx) {
    chartDaily = new Chart(ctx, {
      type:'bar',
      data:{
        labels,
        datasets:[
          {label:'Успешные',data:ok_vals,backgroundColor:'rgba(0,200,80,.6)',borderColor:'rgba(0,200,80,.9)',borderWidth:1},
          {label:'Ошибки',data:err_vals,backgroundColor:'rgba(255,51,51,.5)',borderColor:'rgba(255,51,51,.8)',borderWidth:1},
        ]
      },
      options:{
        responsive:true,maintainAspectRatio:false,
        plugins:{legend:{labels:{color:'#c09090',font:{size:11}}},tooltip:{callbacks:{}}},
        scales:{x:{ticks:{color:'#805050',font:{size:10}},grid:{color:'rgba(255,51,51,.05)'}},
          y:{ticks:{color:'#805050',font:{size:10}},grid:{color:'rgba(255,51,51,.08)'}}}
      }
    });
  }

  // Top commands
  const max = d.top_cmds[0]?.cnt||1;
  document.getElementById('top-cmds').innerHTML = d.top_cmds.map((c,i)=>`
    <div class="top-cmd-row">
      <div>
        <div style="font-size:12px;font-weight:700">${i+1}. ${esc(c.command)}</div>
        <div class="top-cmd-bar"><div class="top-cmd-fill" style="width:${(c.cnt/max*100).toFixed(0)}%"></div></div>
      </div>
      <div style="font-size:14px;font-weight:900;color:var(--a1)">${c.cnt}</div>
    </div>`).join('');

  // Sessions
  const ses = await api('/api/stats/sessions');
  if (ses&&!ses.error) {
    document.getElementById('ses-tbody').innerHTML = ses.slice(0,20).map(s=>`
      <tr>
        <td>${esc(s.ip||'—')}</td>
        <td style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc((s.user_agent||'—').substring(0,60))}</td>
        <td>${s.login_time||'—'}</td>
      </tr>`).join('');
  }
}

// ═══ SETTINGS ════════════════════════════════════════
async function loadSettings() {
  const s = await api('/api/settings');
  if (!s||s.error) return;
  const g = (k,d='') => s[k]||d;
  const t=id=>document.getElementById(id);
  if(t('t-auto'))    t('t-auto').checked    = g('auto_enabled','1')==='1';
  if(t('t-ncmd'))    t('t-ncmd').checked    = g('notify_commands','0')==='1';
  if(t('t-nreg'))    t('t-nreg').checked    = g('notify_registrations','0')==='1';
  if(t('t-chatid'))  t('t-chatid').value    = g('notify_chat_id');
  if(t('t-interval'))t('t-interval').value  = g('check_interval','60');
  // theme
  const theme = g('theme','red');
  applyTheme(theme, false);

  const p = await api('/api/settings/dbpath');
  if (p&&p.path && t('db-path')) t('db-path').textContent = p.path;
}

function sv(k,v){api('/api/settings','POST',{[k]:v}).then(()=>toast('Сохранено','ok'));}
async function saveNotifyChat() {
  const v=document.getElementById('t-chatid').value.trim();
  await api('/api/settings','POST',{notify_chat_id:v}); toast('Chat ID сохранён','ok');
}
async function saveInterval() {
  const v=document.getElementById('t-interval').value;
  await api('/api/settings','POST',{check_interval:v}); toast('Сохранено','ok');
}

function setTheme(t) {
  applyTheme(t, true);
  api('/api/settings','POST',{theme:t});
}

function applyTheme(t, save=false) {
  document.body.className = t==='red' ? '' : 'theme-'+t;
  document.querySelectorAll('.theme-dot').forEach(el => {
    const id = el.id.replace('th-','');
    el.classList.toggle('sel', id===t);
  });
}

// ═══ UTILS ═══════════════════════════════════════════
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function escQ(s){return String(s||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'");}
function escAttr(s){return String(s||'').replace(/"/g,'&quot;');}

function toast(msg, type='info', dur=3000) {
  const c=document.getElementById('toasts');
  const el=document.createElement('div');
  el.className='toast toast-'+type;
  el.textContent=msg; c.appendChild(el);
  setTimeout(()=>el.remove(),dur);
}
// ═══ SITE AUTH ════════════════════════════════════════
let SITE_USER = null;

function switchAuthTab(t) {
  document.getElementById('site-login-form').style.display = t==='login'?'':'none';
  document.getElementById('site-reg-form').style.display   = t==='reg'  ?'':'none';
  document.getElementById('atab-login').classList.toggle('on', t==='login');
  document.getElementById('atab-reg').classList.toggle('on',   t==='reg');
}

async function siteLogin() {
  const nick = document.getElementById('sl-nick').value.trim();
  const pw   = document.getElementById('sl-pw').value.trim();
  const err  = document.getElementById('sl-err');
  err.style.display='none';
  if(!nick||!pw){err.textContent='❌ Заполни оба поля';err.style.display='block';return;}
  const btn = document.getElementById('sl-btn');
  btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  const r = await api('/api/site/login','POST',{nickname:nick,password:pw});
  btn.disabled=false; btn.textContent='🔑 Войти';
  if(r.error){err.textContent='❌ '+r.error;err.style.display='block';return;}
  SITE_USER = r;
  if(r.account_id) AID = r.account_id;
  localStorage.setItem('bfg_user', JSON.stringify(r));
  onSiteLogin();
  toast('Вошёл как '+r.nickname+'!','ok');
}

async function siteRegister() {
  const nick = document.getElementById('sr-nick').value.trim();
  const pw   = document.getElementById('sr-pw').value.trim();
  const err  = document.getElementById('sr-err');
  err.style.display='none';
  if(!nick||!pw){err.textContent='❌ Заполни оба поля';err.style.display='block';return;}
  if(!AID){err.textContent='❌ Сначала авторизуй Telegram ниже';err.style.display='block';return;}
  const btn = document.getElementById('sr-btn');
  btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  const r = await api('/api/site/register','POST',{nickname:nick,password:pw,account_id:AID});
  btn.disabled=false; btn.textContent='📝 Зарегистрироваться';
  if(r.error){err.textContent='❌ '+r.error;err.style.display='block';return;}
  SITE_USER = r;
  if(r.account_id) AID = r.account_id;
  localStorage.setItem('bfg_user', JSON.stringify(r));
  onSiteLogin();
  toast('Зарегистрирован как '+r.nickname+'!','ok');
}

function onSiteLogin() {
  if(!SITE_USER) return;
  // Скрыть формы входа/регистрации
  document.getElementById('site-login-form').style.display='none';
  document.getElementById('site-reg-form').style.display='none';
  document.getElementById('site-logged').style.display='';
  document.getElementById('site-logged-msg').textContent =
    '✅ Вошёл как: '+SITE_USER.nickname+(SITE_USER.is_admin?' 👑 CREATOR':'');
  // Восстановить AID из аккаунта если ещё не задан
  if (!AID && SITE_USER.account_id) {
    AID = SITE_USER.account_id;
  }
  // Обновить chip в topbar
  if (AID) {
    // Обновим chip после загрузки аккаунтов
    loadAccounts();
  }
  // Показать admin вкладку если владелец
  if(SITE_USER.is_admin) {
    document.getElementById('nb-admin').style.display='';
    loadReqs();
  }
}

async function siteLogout() {
  await api('/api/site/logout','POST');
  SITE_USER = null;
  AID = null;
  localStorage.removeItem('bfg_user');
  document.getElementById('site-logged').style.display='none';
  document.getElementById('site-login-form').style.display='';
  document.getElementById('nb-admin').style.display='none';
  const chip = document.getElementById('acc-chip');
  if (chip) chip.style.display = 'none';
  toast('Вышел из аккаунта','info');
}

// ═══ PROFILES ════════════════════════════════════════
async function loadProfiles() {
  const data = await api('/api/site/profiles');
  if(!data||data.error) return;
  const grid = document.getElementById('profiles-grid');
  if(!grid) return;
  grid.innerHTML = data.map(u => {
    const isCr = u.is_admin===1;
    const isBan= u.is_banned===1;
    const nick = (u.nickname||'?').toUpperCase();
    const badge = isCr ? '<span class="prof-badge pb-creator">👑 CREATOR</span>'
                : isBan? '<span class="prof-badge pb-ban">🚫 БАН</span>'
                :        '<span class="prof-badge pb-user">USER</span>';
    return `<div class="prof-card ${isCr?'creator':''}">
      <div class="prof-avatar ${isCr?'cr':''}">${isCr?'👑':nick[0]}</div>
      <div class="prof-nick">${esc(nick)}</div>
      ${badge}
      ${u.tg_name?`<div class="prof-stats">${esc(u.tg_name)}</div>`:''}
      <div class="prof-stats">Задачи: ${u.tasks_cnt||0}</div>
      <div class="prof-stats">${(u.created_at||'').substring(0,10)}</div>
    </div>`;
  }).join('');
}

// ═══ ADMIN ════════════════════════════════════════════
async function loadReqs() {
  const data = await api('/api/admin/reqs');
  const empty = document.getElementById('reqs-empty');
  const list  = document.getElementById('reqs-list');
  if(!list) return;
  if(!data||data.error) { if(empty) empty.textContent='Ошибка загрузки'; return; }
  if(!data.length) {
    if(empty) empty.textContent='Заявок нет';
    list.innerHTML=''; return;
  }
  if(empty) empty.style.display='none';
  list.innerHTML = data.map(u=>`
    <div style="background:var(--bg3);border:1px solid var(--bdr);border-radius:var(--rad);
      padding:11px;margin-bottom:7px">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px">
        <div>
          <div style="font-weight:700;font-size:13px;color:var(--a1)">${esc(u.nickname)}</div>
          <div style="font-size:11px;color:var(--txt3);margin-top:2px">
            ${esc(u.tg_name||'—')} · ${esc(u.phone||'—')}
          </div>
          <div style="font-size:10px;color:var(--txt3)">${(u.created_at||'').substring(0,16)}</div>
        </div>
        <div style="display:flex;gap:5px">
          <button class="btn btn-red btn-sm" onclick="adminBanReq(${u.id},'${esc(u.phone||'')}')">🚫 Забанить</button>
          <button class="btn btn-drk btn-sm" onclick="adminDelReq(${u.id})">🗑 Удалить</button>
        </div>
      </div>
    </div>`).join('');
}

async function adminBanReq(uid, phone) {
  if(phone) await api('/api/admin/ban/phone','POST',{phone,reason:'Отклонён админом'});
  await api('/api/admin/delete/user','POST',{user_id:uid});
  toast('🚫 Заявка отклонена и забанена','ok');
  loadReqs();
}
async function adminDelReq(uid) {
  if(!confirm('Удалить заявку?')) return;
  await api('/api/admin/delete/user','POST',{user_id:uid});
  toast('🗑 Удалено','info');
  loadReqs();
}

async function adminBanPhone() {
  const p=document.getElementById('ban-phone').value.trim();
  const r=document.getElementById('ban-phone-reason').value.trim();
  if(!p){toast('Введи номер','err');return;}
  const res=await api('/api/admin/ban/phone','POST',{phone:p,reason:r});
  res.ok?toast('🚫 '+p+' забанен','ok'):toast('❌ '+(res.error||'Ошибка'),'err');
  loadReqs();
}
async function adminUnbanPhone() {
  const p=document.getElementById('ban-phone').value.trim();
  if(!p){toast('Введи номер','err');return;}
  const res=await api('/api/admin/unban/phone','POST',{phone:p});
  res.ok?toast('✅ Разбанен','ok'):toast('❌ '+(res.error||'Ошибка'),'err');
  loadReqs();
}
async function adminBanIp() {
  const ip=document.getElementById('ban-ip').value.trim();
  const r=document.getElementById('ban-ip-reason').value.trim();
  if(!ip){toast('Введи IP','err');return;}
  const res=await api('/api/admin/ban/ip','POST',{ip,reason:r});
  res.ok?toast('🚫 '+ip+' забанен','ok'):toast('❌ '+(res.error||'Ошибка'),'err');
}
async function adminUnbanIp() {
  const ip=document.getElementById('ban-ip').value.trim();
  const res=await api('/api/admin/unban/ip','POST',{ip});
  res.ok?toast('✅ IP разбанен','ok'):toast('❌ '+(res.error||'Ошибка'),'err');
}
async function adminDelUser() {
  const uid=parseInt(document.getElementById('del-uid').value);
  if(!uid){toast('Введи ID','err');return;}
  if(!confirm('Удалить пользователя #'+uid+'?'))return;
  const res=await api('/api/admin/delete/user','POST',{user_id:uid});
  res.ok?toast('🗑 Удалено','ok'):toast('❌ '+(res.error||'Ошибка'),'err');
  loadReqs();
}
async function loadBanned() {
  const d=await api('/api/admin/banned');
  if(!d||d.error)return;
  let html='';
  if(d.banned_phones.length){
    html+='<b>📵 Забаненные телефоны:</b><br>';
    d.banned_phones.forEach(b=>html+=`<span style="color:var(--a1)">${esc(b.phone)}</span>${b.reason?' ('+b.reason+')':''}<br>`);
    html+='<br>';
  }
  if(d.banned_ips.length){
    html+='<b>🌐 Забаненные IP:</b><br>';
    d.banned_ips.forEach(b=>html+=`<span style="color:var(--a1)">${esc(b.ip)}</span>${b.reason?' ('+b.reason+')':''}<br>`);
  }
  if(!html) html='<span style="color:var(--txt3)">Ничего нет</span>';
  document.getElementById('banned-list').innerHTML=html;
}
async function testNotify(){
  const r=await api('/api/admin/test_notify','POST',{});
  r.ok?toast('✅ Уведомление отправлено! Chat ID: '+r.chat_id,'ok'):toast('❌ '+(r.error||'Ошибка'),'err');
}

// ═══ PATCH go() — добавить profiles/admin ════════════
const _origGo = go;
// Переопределяем go чтобы грузить профили/admin
window._goOrig = go;
go = function(p) {
  _goOrig(p);
  if(p==='admin')   { loadReqs(); }
  if(p==='trading')  { loadTradingSettings(); loadTrades(); loadBtcChart(); }
};

// ═══ TRADING ══════════════════════════════════════════
let _btcChart = null;

async function loadTradingSettings() {
  const d = await api('/api/trading/settings');
  if (!d || d.error) return;
  const el = id => document.getElementById(id);
  if(el('tr-enabled'))  el('tr-enabled').checked  = d.trade_enabled === '1';
  if(el('tr-buy'))      el('tr-buy').value         = d.trade_buy_below  || '';
  if(el('tr-sell'))     el('tr-sell').value        = d.trade_sell_above || '';
  if(el('tr-amount'))   el('tr-amount').value      = d.trade_amount     || '100';
  if(el('tr-interval')) el('tr-interval').value    = d.trade_interval   || '5';
}

async function saveTradingSettings() {
  const d = {
    trade_enabled:    document.getElementById('tr-enabled')?.checked ? '1':'0',
    trade_buy_below:  document.getElementById('tr-buy')?.value    || '0',
    trade_sell_above: document.getElementById('tr-sell')?.value   || '0',
    trade_amount:     document.getElementById('tr-amount')?.value || '100',
    trade_interval:   document.getElementById('tr-interval')?.value|| '5',
  };
  const r = await api('/api/trading/settings','POST',d);
  r.ok ? toast('Настройки трейдинга сохранены','ok') : toast('Ошибка','err');
}

async function checkBtcNow() {
  if (!AID) { toast('Выбери аккаунт','err'); return; }
  toast('Проверяю курс BTC...','info');
  const r = await api('/api/command/run','POST',{account_id:AID, command_text:'Биткоин курс'});
  if (r.error) { toast('❌ '+r.error,'err'); return; }
  document.getElementById('btc-cur').textContent = r.text?.substring(0,60) || '—';
  toast('Курс получен','ok');
  setTimeout(loadBtcChart, 1000);
}

async function loadBtcChart() {
  const hist = await api('/api/trading/history');
  if (!hist || !hist.length) return;
  const labels = hist.map(h => h.ts);
  const prices = hist.map(h => h.price);
  if (prices.length) {
    document.getElementById('btc-cur').textContent = prices[prices.length-1].toLocaleString() + ' BFG';
  }
  if (_btcChart) _btcChart.destroy();
  const ctx = document.getElementById('chart-btc');
  if (!ctx) return;
  _btcChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'BTC курс (BFG)',
        data: prices,
        borderColor: 'rgba(255,51,51,.9)',
        backgroundColor: 'rgba(255,51,51,.1)',
        borderWidth: 2,
        fill: true,
        tension: 0.4,
        pointRadius: 2,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color:'#c09090', font:{size:11} } } },
      scales: {
        x: { ticks:{color:'#805050',font:{size:9}}, grid:{color:'rgba(255,51,51,.05)'} },
        y: { ticks:{color:'#805050',font:{size:9}}, grid:{color:'rgba(255,51,51,.08)'} }
      }
    }
  });
}

async function loadTrades() {
  const data = await api('/api/trading/trades');
  const el = document.getElementById('trades-list');
  if (!el) return;
  if (!data || !data.length) {
    el.innerHTML = '<div style="text-align:center;padding:20px;color:var(--txt3)">Нет сделок</div>';
    return;
  }
  el.innerHTML = data.map(t => `
    <div class="trade-item">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">
        <div>
          <span class="${t.action==='buy'?'trade-buy':'trade-sell'}" style="font-weight:700;font-size:13px">
            ${t.action==='buy'?'🟢 КУПИЛ':'🔴 ПРОДАЛ'}
          </span>
          <span style="color:var(--txt3);font-size:11px;margin-left:8px">${esc(t.tg_name||t.phone||'')}</span>
        </div>
        <div style="text-align:right">
          <div style="font-size:12px;color:var(--a1);font-weight:700">@ ${esc(String(t.price))}</div>
          <div style="font-size:10px;color:var(--txt3)">${esc(t.timestamp||'')}</div>
        </div>
      </div>
      <div style="font-size:11px;color:var(--txt3);margin-top:4px">Объём: ${esc(String(t.amount))}</div>
      ${t.result?`<div style="font-size:11px;color:var(--txt2);margin-top:3px">${esc(t.result.substring(0,100))}</div>`:''}
    </div>`).join('');
}

</script>
</body>
</html>"""

@app.route('/')
def index():
    return Response(HTML, mimetype='text/html; charset=utf-8')

# ── MAIN ────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print(f"\n{'='*50}")
    print(f"  BFG AUTO PLATFORM v3.0")
    print(f"  База: {DB}")
    print(f"  http://localhost:5000")
    print(f"{'='*50}\n")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
