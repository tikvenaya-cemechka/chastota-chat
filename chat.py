import sqlite3
import random
import string
import time
import secrets
from flask import Flask, render_template_string, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

app = Flask(__name__)
DB_PATH = 'chastota.db'
ONLINE_SECONDS = 12 # если человек делал запрос за последние N секунд — считаем "в сети"
TYPING_SECONDS = 3

FOUNDER_USERNAMES = [] # впиши сюда свой юзернейм и юзернеймы друзей, когда будете готовы


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        name TEXT, surname TEXT, password_hash TEXT, bio TEXT,
        avatar TEXT DEFAULT '😀', avatar_photo TEXT, last_active TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user TEXT, to_user TEXT, text TEXT, time TEXT, read INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS aliases (
        owner TEXT, contact TEXT, alias TEXT, PRIMARY KEY (owner, contact)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, username TEXT
    )''')
    conn.commit()
    conn.close()


init_db()

pending_regs = {} # reg_id -> {name, surname, username, password, code, expires}
last_typing = {} # (from_user, to_user) -> timestamp


# ---------- helpers ----------

def get_user(username):
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def touch_active(username):
    conn = get_db()
    conn.execute('UPDATE users SET last_active=? WHERE username=?', (datetime.now().isoformat(), username))
    conn.commit()
    conn.close()


def is_official(username):
    return username.lower() in [f.lower() for f in FOUNDER_USERNAMES]


def get_status(u):
    if not u.get('last_active'):
        return {'online': False, 'last_active': None}
    delta = (datetime.now() - datetime.fromisoformat(u['last_active'])).total_seconds()
    return {'online': delta < ONLINE_SECONDS, 'last_active': u['last_active']}


def get_alias(owner, contact):
    conn = get_db()
    row = conn.execute('SELECT alias FROM aliases WHERE owner=? AND contact=?', (owner, contact)).fetchone()
    conn.close()
    return row['alias'] if row else None


def public_user(u, viewer=None):
    status = get_status(u)
    name = u['name']
    if viewer:
        alias = get_alias(viewer, u['username'])
        if alias:
            name = alias
    return {
        'username': u['username'], 'name': name, 'avatar': u['avatar'],
        'avatar_photo': u.get('avatar_photo'), 'bio': u.get('bio') or '',
        'official': is_official(u['username']),
        'online': status['online'], 'last_active': status['last_active']
    }


def get_contacts(username):
    conn = get_db()
    rows = conn.execute('''
        SELECT DISTINCT CASE WHEN from_user=? THEN to_user ELSE from_user END AS other
        FROM messages WHERE from_user=? OR to_user=?
    ''', (username, username, username)).fetchall()
    conn.close()
    contacts = []
    for r in rows:
        u = get_user(r['other'])
        if u:
            contacts.append(public_user(u, viewer=username))
    return contacts


def session_user(token):
    if not token:
        return None
    conn = get_db()
    row = conn.execute('SELECT username FROM sessions WHERE token=?', (token,)).fetchone()
    conn.close()
    if not row:
        return None
    return get_user(row['username'])


def require_auth():
    token = request.args.get('token') or (request.json or {}).get('token')
    u = session_user(token)
    return u


# ---------- API ----------

@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.json or {}
    username = data.get('username', '').strip().lstrip('@')
    name = data.get('name', '').strip()
    surname = data.get('surname', '').strip()
    password = data.get('password', '')
    if not username or not name or not password:
        return jsonify({'error': 'Заполни имя, юзернейм и пароль'}), 400
    if get_user(username):
        return jsonify({'error': 'Этот юзернейм уже занят'}), 400
    code = ''.join(random.choices(string.digits, k=5))
    reg_id = secrets.token_hex(8)
    pending_regs[reg_id] = {
        'username': username, 'name': name, 'surname': surname,
        'password': password, 'code': code, 'expires': time.time() + 300
    }
    return jsonify({'reg_id': reg_id, 'code': code})


@app.route('/api/confirm_captcha', methods=['POST'])
def api_confirm_captcha():
    data = request.json or {}
    reg_id = data.get('reg_id')
    entered = data.get('code', '').strip()
    reg = pending_regs.get(reg_id)
    if not reg or reg['expires'] < time.time():
        return jsonify({'error': 'Сессия истекла, попробуй заново'}), 400
    if entered != reg['code']:
        return jsonify({'error': 'Код неверный, попробуй ещё раз'}), 400
    conn = get_db()
    conn.execute(
        'INSERT INTO users (username, name, surname, password_hash, bio, avatar, last_active) VALUES (?,?,?,?,?,?,?)',
        (reg['username'], reg['name'], reg['surname'], generate_password_hash(reg['password']),
         '', '😀', datetime.now().isoformat())
    )
    token = secrets.token_hex(24)
    conn.execute('INSERT INTO sessions (token, username) VALUES (?,?)', (token, reg['username']))
    conn.commit()
    conn.close()
    del pending_regs[reg_id]
    u = get_user(reg['username'])
    return jsonify({'token': token, 'user': public_user(u), 'contacts': []})


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json or {}
    username = data.get('username', '').strip().lstrip('@')
    password = data.get('password', '')
    u = get_user(username)
    if not u or not check_password_hash(u['password_hash'], password):
        return jsonify({'error': 'Неверный юзернейм или пароль'}), 400
    token = secrets.token_hex(24)
    conn = get_db()
    conn.execute('INSERT INTO sessions (token, username) VALUES (?,?)', (token, username))
    conn.commit()
    conn.close()
    touch_active(username)
    return jsonify({'token': token, 'user': public_user(u), 'contacts': get_contacts(username)})


@app.route('/api/me')
def api_me():
    u = require_auth()
    if not u:
        return jsonify({'error': 'Сессия не найдена'}), 401
    touch_active(u['username'])
    return jsonify({'user': public_user(u), 'contacts': get_contacts(u['username'])})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    data = request.json or {}
    token = data.get('token')
    conn = get_db()
    conn.execute('DELETE FROM sessions WHERE token=?', (token,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/find_user')
def api_find_user():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    username = request.args.get('username', '').strip().lstrip('@')
    u = get_user(username)
    if not u:
        return jsonify({'found': False})
    return jsonify({'found': True, 'user': public_user(u, viewer=me['username'])})


@app.route('/api/send_message', methods=['POST'])
def api_send_message():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    to_user = data.get('to', '').strip()
    text = data.get('text', '')
    if not get_user(to_user) or not text.strip():
        return jsonify({'error': 'bad request'}), 400
    time_str = datetime.now().strftime('%H:%M')
    conn = get_db()
    cur = conn.execute('INSERT INTO messages (from_user, to_user, text, time) VALUES (?,?,?,?)',
                        (me['username'], to_user, text, time_str))
    conn.commit()
    msg_id = cur.lastrowid
    conn.close()
    touch_active(me['username'])
    return jsonify({'id': msg_id, 'from_user': me['username'], 'to_user': to_user, 'text': text,
                     'time': time_str, 'read': 0})


@app.route('/api/typing', methods=['POST'])
def api_typing():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    to_user = data.get('to', '')
    last_typing[(me['username'], to_user)] = time.time()
    return jsonify({'ok': True})


@app.route('/api/sync')
def api_sync():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    touch_active(me['username'])
    since_id = int(request.args.get('since_id', 0))
    with_user = request.args.get('with', '')

    conn = get_db()
    rows = conn.execute('''
        SELECT id, from_user, to_user, text, time, read FROM messages
        WHERE id > ? AND (from_user=? OR to_user=?) ORDER BY id ASC
    ''', (since_id, me['username'], me['username'])).fetchall()
    new_messages = [dict(r) for r in rows]

    read_up_to_id = 0
    typing = False
    if with_user:
        conn.execute('UPDATE messages SET read=1 WHERE from_user=? AND to_user=? AND read=0',
                     (with_user, me['username']))
        conn.commit()
        row = conn.execute('SELECT MAX(id) AS m FROM messages WHERE from_user=? AND to_user=? AND read=1',
                            (me['username'], with_user)).fetchone()
        read_up_to_id = row['m'] or 0
        ts = last_typing.get((with_user, me['username']))
        typing = bool(ts and time.time() - ts < TYPING_SECONDS)
    conn.close()

    max_id = since_id
    if new_messages:
        max_id = max(m['id'] for m in new_messages)

    return jsonify({
        'new_messages': new_messages,
        'contacts': get_contacts(me['username']),
        'read_up_to_id': read_up_to_id,
        'typing': typing,
        'max_id': max_id
    })


@app.route('/api/open_chat')
def api_open_chat():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    with_user = request.args.get('with', '')
    conn = get_db()
    rows = conn.execute('''
        SELECT id, from_user, to_user, text, time, read FROM messages
        WHERE (from_user=? AND to_user=?) OR (from_user=? AND to_user=?)
        ORDER BY id ASC
    ''', (me['username'], with_user, with_user, me['username'])).fetchall()
    conn.execute('UPDATE messages SET read=1 WHERE from_user=? AND to_user=? AND read=0',
                 (with_user, me['username']))
    conn.commit()
    conn.close()
    max_id = max([r['id'] for r in rows], default=0)
    return jsonify({'messages': [dict(r) for r in rows], 'max_id': max_id})


@app.route('/api/update_bio', methods=['POST'])
def api_update_bio():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    bio = (request.json or {}).get('bio', '')
    conn = get_db()
    conn.execute('UPDATE users SET bio=? WHERE username=?', (bio, me['username']))
    conn.commit()
    conn.close()
    return jsonify({'bio': bio})


@app.route('/api/update_avatar', methods=['POST'])
def api_update_avatar():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    avatar_photo = (request.json or {}).get('avatar_photo')
    conn = get_db()
    conn.execute('UPDATE users SET avatar_photo=? WHERE username=?', (avatar_photo, me['username']))
    conn.commit()
    conn.close()
    return jsonify({'avatar_photo': avatar_photo})


@app.route('/api/set_alias', methods=['POST'])
def api_set_alias():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    contact = data.get('contact')
    alias = data.get('alias', '').strip()
    conn = get_db()
    if alias:
        conn.execute('INSERT OR REPLACE INTO aliases (owner, contact, alias) VALUES (?,?,?)',
                     (me['username'], contact, alias))
    else:
        conn.execute('DELETE FROM aliases WHERE owner=? AND contact=?', (me['username'], contact))
    conn.commit()
    conn.close()
    u = get_user(contact)
    return jsonify({'contact': contact, 'name': alias if alias else (u['name'] if u else contact)})


# ---------- страница ----------

PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Частота</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #12161f; --panel: #1b2130; --panel-raised: #232b3d;
    --accent: #ffb84d; --signal: #5eead4; --text: #e8e6e3;
    --text-dim: #8b93a7; --border: #2a3245; --danger: #ff6b6b;
  }
  body.light {
    --bg: #f5f3ef; --panel: #ffffff; --panel-raised: #eeecea;
    --accent: #d97706; --signal: #0d9488; --text: #1b1f27;
    --text-dim: #6b7280; --border: #dcdad5; --danger: #dc2626;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
  .screen { position: fixed; inset: 0; display: none; flex-direction: column; background: var(--bg); }
  .screen.active { display: flex; }
  .center { align-items: center; justify-content: center; gap: 16px; padding: 24px; text-align: center; }
  .logo { font-family: 'Fraunces', serif; font-weight: 700; font-size: 40px; letter-spacing: -0.02em; }
  .logo .dot { color: var(--accent); }
  .tagline { font-family: 'IBM Plex Mono', monospace; color: var(--text-dim); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; margin-top: 2px; }
  input {
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    font-family: 'IBM Plex Mono', monospace; font-size: 15px; padding: 12px 16px;
    border-radius: 10px; width: 260px; outline: none; transition: border-color 0.2s;
  }
  input:focus { border-color: var(--accent); }
  button.primary {
    background: var(--accent); color: #1b1204; font-weight: 600; font-size: 15px;
    padding: 12px 30px; border: none; border-radius: 10px; cursor: pointer; transition: opacity 0.15s;
  }
  button.primary:hover { opacity: 0.9; }
  .link-btn { background: none; border: none; color: var(--signal); cursor: pointer; font-size: 13.5px; text-decoration: underline; }
  .error-msg { color: var(--danger); font-family: 'IBM Plex Mono', monospace; font-size: 12.5px; min-height: 16px; }
  .captcha-code { font-family: 'IBM Plex Mono', monospace; font-size: 34px; letter-spacing: 10px; background: var(--panel-raised); padding: 14px 24px; border-radius: 12px; color: var(--accent); }

  header { display: flex; align-items: center; justify-content: space-between; padding: 14px 20px; border-bottom: 1px solid var(--border); background: var(--panel); }
  .brand { font-family: 'Fraunces', serif; font-weight: 700; font-size: 19px; }
  .brand .dot { color: var(--accent); }
  .header-right { display: flex; align-items: center; gap: 10px; }
  .icon-btn { background: none; border: 1px solid var(--border); color: var(--text-dim); border-radius: 8px; padding: 6px 10px; font-size: 12px; cursor: pointer; font-family: 'Inter', sans-serif; }
  .icon-btn:hover { color: var(--text); border-color: var(--accent); }

  .search-block { padding: 16px 20px; border-bottom: 1px solid var(--border); }
  .search-row { display: flex; gap: 8px; }
  .search-row input { flex: 1; width: auto; }
  .search-row button { background: var(--accent); color: #1b1204; border: none; border-radius: 10px; padding: 0 20px; font-weight: 600; cursor: pointer; }
  #searchError { color: var(--danger); font-family: 'IBM Plex Mono', monospace; font-size: 12px; margin-top: 8px; min-height: 16px; }

  .contacts-title { padding: 12px 20px 6px; font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.06em; }
  .contacts-list { flex: 1; overflow-y: auto; padding: 0 10px 10px; }
  .contact-item { display: flex; align-items: center; gap: 12px; padding: 12px; border-radius: 12px; cursor: pointer; transition: background 0.15s; }
  .contact-item:hover { background: var(--panel-raised); }
  .avatar-box { width: 42px; height: 42px; border-radius: 12px; background: var(--panel-raised); display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; overflow: hidden; }
  .avatar-box img { width: 100%; height: 100%; object-fit: cover; }
  .contact-name { font-weight: 600; font-size: 14.5px; display: inline-flex; align-items: center; gap: 4px; }
  .official-badge { display: inline-flex; width: 15px; height: 15px; flex-shrink: 0; }
  .contact-username { font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; color: var(--text-dim); }
  .contact-status { font-family: 'IBM Plex Mono', monospace; font-size: 10.5px; margin-top: 1px; }
  .status-online { color: #3ba7f5; }
  .status-offline { color: var(--text-dim); }
  .empty-hint { padding: 20px; color: var(--text-dim); font-size: 13.5px; text-align: center; }

  .back-btn { background: none; border: none; color: var(--text); font-size: 20px; cursor: pointer; padding: 0 8px 0 0; }
  .chat-header-info { display: flex; align-items: center; gap: 10px; flex: 1; }
  #chatTyping { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--accent); min-height: 14px; }
  #messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 4px; }
  .msg { max-width: 78%; animation: rise 0.18s ease-out; padding: 2px 0; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
  .msg .meta { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--text-dim); margin-bottom: 3px; }
  .ticks { margin-left: 5px; color: var(--text-dim); }
  .ticks.read { color: #3ba7f5; }
  .msg .bubble { background: var(--panel-raised); border-radius: 4px 12px 12px 12px; padding: 10px 14px; font-size: 14.5px; line-height: 1.45; word-wrap: break-word; }
  .msg.own { align-self: flex-end; }
  .msg.own .meta { text-align: right; }
  .msg.own .bubble { background: var(--accent); color: #1b1204; border-radius: 12px 4px 12px 12px; }
  #composer { display: flex; gap: 10px; padding: 16px 20px; border-top: 1px solid var(--border); background: var(--panel); }
  #textInput { flex: 1; width: auto; }
  #sendBtn { background: var(--accent); color: #1b1204; border: none; border-radius: 10px; padding: 0 22px; font-weight: 600; cursor: pointer; }

  .bio-box { width: 280px; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; text-align: left; }
  textarea { width: 100%; background: var(--panel-raised); border: 1px solid var(--border); color: var(--text); font-family: 'Inter', sans-serif; font-size: 14px; padding: 10px; border-radius: 8px; resize: vertical; min-height: 60px; }

  #messages::-webkit-scrollbar, .contacts-list::-webkit-scrollbar { width: 6px; }
  #messages::-webkit-scrollbar-thumb, .contacts-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<div id="registerScreen" class="screen active center">
  <div><div class="logo">Частота<span class="dot">.</span></div><div class="tagline">создать аккаунт</div></div>
  <input type="text" id="regName" placeholder="Имя" maxlength="20">
  <input type="text" id="regSurname" placeholder="Фамилия (необязательно)" maxlength="20">
  <input type="text" id="regUsername" placeholder="Юзернейм (@nickname)" maxlength="20">
  <input type="password" id="regPassword" placeholder="Пароль">
  <button class="primary" id="regBtn">Продолжить</button>
  <div class="error-msg" id="regError"></div>
  <button class="link-btn" id="toLoginBtn">Уже есть аккаунт? Войти</button>
</div>

<div id="captchaScreen" class="screen center">
  <div class="logo">Частота<span class="dot">.</span></div>
  <div class="tagline">подтверди, что ты не бот</div>
  <div class="captcha-code" id="captchaCode"></div>
  <input type="text" id="captchaInput" placeholder="Введи код" maxlength="5">
  <button class="primary" id="captchaBtn">Подтвердить</button>
  <div class="error-msg" id="captchaError"></div>
</div>

<div id="loginScreen" class="screen center">
  <div><div class="logo">Частота<span class="dot">.</span></div><div class="tagline">вход в аккаунт</div></div>
  <input type="text" id="loginUsername" placeholder="Юзернейм (@nickname)">
  <input type="password" id="loginPassword" placeholder="Пароль">
  <button class="primary" id="loginBtn">Войти</button>
  <div class="error-msg" id="loginError"></div>
  <button class="link-btn" id="toRegisterBtn">Нет аккаунта? Создать</button>
</div>

<div id="dashScreen" class="screen">
  <header>
    <div class="brand">Частота<span class="dot">.</span></div>
    <div class="header-right">
      <button class="icon-btn" id="themeBtn">🌙</button>
      <button class="icon-btn" id="bioBtn">О себе</button>
      <button class="icon-btn" id="logoutBtn">Выйти</button>
    </div>
  </header>
  <div class="search-block">
    <div class="search-row">
      <input type="text" id="searchInput" placeholder="Юзернейм собеседника (@nickname)">
      <button id="searchBtn">Найти</button>
    </div>
    <div id="searchError"></div>
  </div>
  <div class="contacts-title">Недавние переписки</div>
  <div class="contacts-list" id="contactsList"></div>
</div>

<div id="bioScreen" class="screen center">
  <div class="bio-box">
    <div class="brand" style="margin-bottom:12px;">Профиль</div>
    <div style="display:flex; align-items:center; gap:12px; margin-bottom:14px;">
      <div class="avatar-box" id="avatarPreview" style="width:60px;height:60px;font-size:28px;"></div>
      <div style="display:flex; flex-direction:column; gap:6px;">
        <label class="icon-btn" style="text-align:center; cursor:pointer;">
          Загрузить фото
          <input type="file" id="avatarFileInput" accept="image/*" style="display:none;">
        </label>
        <button class="icon-btn" id="avatarRemoveBtn">Убрать фото</button>
      </div>
    </div>
    <textarea id="bioInput" placeholder="Расскажи что-нибудь о себе..."></textarea>
    <div style="display:flex; gap:8px; margin-top:12px;">
      <button class="primary" id="bioSaveBtn" style="flex:1;">Сохранить</button>
      <button class="icon-btn" id="bioBackBtn">Назад</button>
    </div>
  </div>
</div>

<div id="chatScreen" class="screen">
  <header>
    <button class="back-btn" id="backBtn">←</button>
    <div class="chat-header-info">
      <div class="avatar-box" id="chatAvatar"></div>
      <div>
        <div class="brand" id="chatName" style="font-size:16px;"></div>
        <div class="contact-username" id="chatUsername"></div>
        <div id="chatTyping"></div>
      </div>
    </div>
    <button class="icon-btn" id="renameBtn">✏️</button>
  </header>
  <div id="messages"></div>
  <div id="composer">
    <input type="text" id="textInput" placeholder="Сообщение...">
    <button id="sendBtn">Отправить</button>
  </div>
</div>

<script>
  let me = null;
  let token = localStorage.getItem('chastota_token') || null;
  let currentContact = null;
  let contactsCache = [];
  let sinceId = 0;
  let pendingRegId = null;
  let pollTimer = null;

  // --- Тема ---
  function applyTheme(theme) {
    document.body.classList.toggle('light', theme === 'light');
    const btn = document.getElementById('themeBtn');
    if (btn) btn.textContent = theme === 'light' ? '☀️' : '🌙';
  }
  applyTheme(localStorage.getItem('chastota_theme') || 'dark');
  document.getElementById('themeBtn').addEventListener('click', () => {
    const next = document.body.classList.contains('light') ? 'dark' : 'light';
    applyTheme(next);
    localStorage.setItem('chastota_theme', next);
  });

  function showScreen(id) {
    document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
    document.getElementById(id).classList.add('active');
  }
  function escapeHtml(str) {
    const d = document.createElement('div'); d.textContent = str; return d.innerHTML;
  }
  function officialBadge(isOfficial) {
    if (!isOfficial) return '';
    return '<svg class="official-badge" viewBox="0 0 22 22" title="Official"><path fill="#3ba7f5" d="M11 0l2.2 2.1 3-.7 1 2.9 3 1-0.7 3L21 11l-2.1 2.2.7 3-2.9 1-1 3-3-.7L11 22l-2.2-2.1-3 .7-1-2.9-3-1 .7-3L0 11l2.1-2.2-.7-3 2.9-1 1-3 3 .7z"/><path fill="#fff" d="M9.3 14.7L6 11.4l1.4-1.4 1.9 1.9 4.9-4.9 1.4 1.4z"/></svg>';
  }
  function avatarHtml(user) {
    if (user && user.avatar_photo) return '<img src="' + user.avatar_photo + '">';
    return (user && user.avatar) ? user.avatar : '😀';
  }
  function statusInfo(user) {
    if (user.online) return { text: 'В сети', cls: 'status-online' };
    if (!user.last_active) return { text: '', cls: '' };
    const days = (Date.now() - new Date(user.last_active).getTime()) / 86400000;
    return days < 30 ? { text: 'был(а) недавно', cls: 'status-offline' } : { text: 'был(а) давно', cls: 'status-offline' };
  }

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = { 'Content-Type': 'application/json' };
    if (opts.body) opts.body = JSON.stringify(Object.assign({ token }, opts.body));
    const sep = path.includes('?') ? '&' : '?';
    const url = opts.body ? path : path + sep + 'token=' + encodeURIComponent(token || '');
    const res = await fetch(url, opts);
    return { ok: res.ok, data: await res.json() };
  }

  // --- Автовход при загрузке ---
  window.addEventListener('load', async () => {
    if (token) {
      const r = await api('/api/me');
      if (r.ok) {
        me = r.data.user; contactsCache = r.data.contacts;
        renderContacts(contactsCache);
        showScreen('dashScreen');
        startPolling();
        return;
      } else {
        localStorage.removeItem('chastota_token'); token = null;
      }
    }
    showScreen('registerScreen');
  });

  // --- Регистрация ---
  document.getElementById('regBtn').addEventListener('click', async () => {
    const name = document.getElementById('regName').value.trim();
    const surname = document.getElementById('regSurname').value.trim();
    const username = document.getElementById('regUsername').value.trim().replace('@', '');
    const password = document.getElementById('regPassword').value;
    const errEl = document.getElementById('regError'); errEl.textContent = '';
    if (!name || !username || !password) { errEl.textContent = 'Заполни имя, юзернейм и пароль'; return; }
    const r = await api('/api/register', { method: 'POST', body: { name, surname, username, password } });
    if (!r.ok) { errEl.textContent = r.data.error; return; }
    pendingRegId = r.data.reg_id;
    document.getElementById('captchaCode').textContent = r.data.code;
    document.getElementById('captchaInput').value = '';
    document.getElementById('captchaError').textContent = '';
    showScreen('captchaScreen');
  });
  document.getElementById('captchaBtn').addEventListener('click', async () => {
    const code = document.getElementById('captchaInput').value.trim();
    const r = await api('/api/confirm_captcha', { method: 'POST', body: { reg_id: pendingRegId, code } });
    if (!r.ok) { document.getElementById('captchaError').textContent = r.data.error; return; }
    onAuthSuccess(r.data);
  });
  document.getElementById('toLoginBtn').addEventListener('click', () => showScreen('loginScreen'));
  document.getElementById('toRegisterBtn').addEventListener('click', () => showScreen('registerScreen'));

  // --- Вход ---
  document.getElementById('loginBtn').addEventListener('click', async () => {
    const username = document.getElementById('loginUsername').value.trim().replace('@', '');
    const password = document.getElementById('loginPassword').value;
    const r = await api('/api/login', { method: 'POST', body: { username, password } });
    if (!r.ok) { document.getElementById('loginError').textContent = r.data.error; return; }
    onAuthSuccess(r.data);
  });

  function onAuthSuccess(data) {
    token = data.token;
    localStorage.setItem('chastota_token', token);
    me = data.user;
    contactsCache = data.contacts || [];
    renderContacts(contactsCache);
    showScreen('dashScreen');
    startPolling();
  }

  document.getElementById('logoutBtn').addEventListener('click', async () => {
    stopPolling();
    await api('/api/logout', { method: 'POST', body: {} });
    localStorage.removeItem('chastota_token');
    token = null; me = null; currentContact = null; sinceId = 0;
    document.getElementById('loginUsername').value = '';
    document.getElementById('loginPassword').value = '';
    showScreen('loginScreen');
  });

  function renderContacts(contacts) {
    const list = document.getElementById('contactsList');
    list.innerHTML = '';
    if (!contacts.length) {
      list.innerHTML = '<div class="empty-hint">Пока нет переписок. Введи юзернейм выше, чтобы начать.</div>';
      return;
    }
    contacts.forEach(c => {
      const item = document.createElement('div');
      item.className = 'contact-item';
      const st = statusInfo(c);
      item.innerHTML = '<div class="avatar-box">' + avatarHtml(c) + '</div><div><div class="contact-name">' + escapeHtml(c.name) + officialBadge(c.official) + '</div><div class="contact-username">@' + escapeHtml(c.username) + '</div><div class="contact-status ' + st.cls + '">' + st.text + '</div></div>';
      item.addEventListener('click', () => openChat(c));
      list.appendChild(item);
    });
  }

  // --- Поиск ---
  document.getElementById('searchBtn').addEventListener('click', async () => {
    const username = document.getElementById('searchInput').value.trim().replace('@', '');
    const errEl = document.getElementById('searchError'); errEl.textContent = '';
    if (!username) return;
    if (username === me.username) { errEl.textContent = 'Это твой собственный юзернейм'; return; }
    const r = await api('/api/find_user?username=' + encodeURIComponent(username));
    if (r.data.found) { openChat(r.data.user); } else { errEl.textContent = 'Юзернейм не найден'; }
  });

  // --- Профиль ---
  document.getElementById('bioBtn').addEventListener('click', () => {
    document.getElementById('bioInput').value = me.bio || '';
    document.getElementById('avatarPreview').innerHTML = avatarHtml(me);
    showScreen('bioScreen');
  });
  document.getElementById('bioBackBtn').addEventListener('click', () => showScreen('dashScreen'));
  document.getElementById('bioSaveBtn').addEventListener('click', async () => {
    const bio = document.getElementById('bioInput').value.trim();
    const r = await api('/api/update_bio', { method: 'POST', body: { bio } });
    me.bio = r.data.bio;
    showScreen('dashScreen');
  });
  document.getElementById('avatarFileInput').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    resizeImage(file, 300).then(async dataUrl => {
      const r = await api('/api/update_avatar', { method: 'POST', body: { avatar_photo: dataUrl } });
      me.avatar_photo = r.data.avatar_photo;
      document.getElementById('avatarPreview').innerHTML = avatarHtml(me);
    }).catch(() => alert('Не получилось загрузить фото, попробуй другое'));
  });
  document.getElementById('avatarRemoveBtn').addEventListener('click', async () => {
    const r = await api('/api/update_avatar', { method: 'POST', body: { avatar_photo: null } });
    me.avatar_photo = r.data.avatar_photo;
    document.getElementById('avatarPreview').innerHTML = avatarHtml(me);
  });
  function resizeImage(file, maxSize) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = reject;
      reader.onload = () => {
        const img = new Image();
        img.onerror = reject;
        img.onload = () => {
          let w = img.width, h = img.height;
          if (w > h && w > maxSize) { h = h * (maxSize / w); w = maxSize; }
          else if (h > maxSize) { w = w * (maxSize / h); h = maxSize; }
          const canvas = document.createElement('canvas');
          canvas.width = w; canvas.height = h;
          canvas.getContext('2d').drawImage(img, 0, 0, w, h);
          resolve(canvas.toDataURL('image/jpeg', 0.8));
        };
        img.src = reader.result;
      };
      reader.readAsDataURL(file);
    });
  }

  // --- Чат ---
  async function openChat(contact) {
    currentContact = contact;
    if (!contactsCache.find(c => c.username === contact.username)) contactsCache.unshift(contact);
    document.getElementById('chatAvatar').innerHTML = avatarHtml(contact);
    document.getElementById('chatName').innerHTML = escapeHtml(contact.name) + officialBadge(contact.official);
    renderChatStatus(contact);
    document.getElementById('chatTyping').textContent = '';
    document.getElementById('messages').innerHTML = '';
    showScreen('chatScreen');
    const r = await api('/api/open_chat?with=' + encodeURIComponent(contact.username));
    r.data.messages.forEach(renderMessage);
    sinceId = Math.max(sinceId, r.data.max_id);
  }
  function renderChatStatus(contact) {
    const st = statusInfo(contact);
    document.getElementById('chatUsername').innerHTML = '@' + escapeHtml(contact.username) +
      (st.text ? ' · <span class="' + st.cls + '">' + st.text + '</span>' : '');
  }
  document.getElementById('backBtn').addEventListener('click', () => { currentContact = null; showScreen('dashScreen'); });

  document.getElementById('renameBtn').addEventListener('click', async () => {
    if (!currentContact) return;
    const newName = prompt('Как назвать этот контакт (видно только тебе):', currentContact.name);
    if (newName === null) return;
    const r = await api('/api/set_alias', { method: 'POST', body: { contact: currentContact.username, alias: newName.trim() } });
    currentContact.name = r.data.name;
    document.getElementById('chatName').innerHTML = escapeHtml(currentContact.name) + officialBadge(currentContact.official);
    const idx = contactsCache.findIndex(c => c.username === r.data.contact);
    if (idx !== -1) contactsCache[idx].name = r.data.name;
  });

  function renderMessage(msg) {
    const div = document.createElement('div');
    const isOwn = msg.from_user === me.username;
    div.className = 'msg' + (isOwn ? ' own' : '');
    div.dataset.id = msg.id;
    const ticks = isOwn ? ('<span class="ticks' + (msg.read ? ' read' : '') + '">' + (msg.read ? '✓✓' : '✓') + '</span>') : '';
    div.innerHTML = '<div class="meta">' + msg.time + ticks + '</div><div class="bubble">' + escapeHtml(msg.text) + '</div>';
    document.getElementById('messages').appendChild(div);
    document.getElementById('messages').scrollTop = document.getElementById('messages').scrollHeight;
  }

  async function send() {
    const input = document.getElementById('textInput');
    const text = input.value.trim();
    if (!text || !currentContact) return;
    input.value = '';
    const r = await api('/api/send_message', { method: 'POST', body: { to: currentContact.username, text } });
    if (r.ok) { renderMessage(r.data); sinceId = Math.max(sinceId, r.data.id); }
  }
  document.getElementById('sendBtn').addEventListener('click', send);
  document.getElementById('textInput').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });

  let lastTypingSent = 0;
  document.getElementById('textInput').addEventListener('input', () => {
    if (!currentContact) return;
    const now = Date.now();
    if (now - lastTypingSent > 1500) {
      lastTypingSent = now;
      api('/api/typing', { method: 'POST', body: { to: currentContact.username } });
    }
  });

  // --- Опрос сервера (замена WebSocket) ---
  function startPolling() {
    stopPolling();
    pollTimer = setInterval(pollOnce, 2500);
    pollOnce();
  }
  function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

  async function pollOnce() {
    if (!token) return;
    const withParam = currentContact ? '&with=' + encodeURIComponent(currentContact.username) : '';
    const r = await api('/api/sync?since_id=' + sinceId + withParam);
    if (!r.ok) return;
    contactsCache = r.data.contacts;
    if (document.getElementById('dashScreen').classList.contains('active')) renderContacts(contactsCache);

    r.data.new_messages.forEach(m => {
      if (currentContact && document.getElementById('chatScreen').classList.contains('active') &&
          ((m.from_user === currentContact.username && m.to_user === me.username) ||
           (m.from_user === me.username && m.to_user === currentContact.username))) {
        if (!document.querySelector('.msg[data-id="' + m.id + '"]')) renderMessage(m);
      }
    });
    if (r.data.max_id > sinceId) sinceId = r.data.max_id;

    if (currentContact) {
      document.querySelectorAll('#messages .msg.own').forEach(el => {
        if (parseInt(el.dataset.id) <= r.data.read_up_to_id) {
          const t = el.querySelector('.ticks');
          if (t) { t.textContent = '✓✓'; t.classList.add('read'); }
        }
      });
      document.getElementById('chatTyping').textContent = r.data.typing ? 'печатает...' : '';
      const updated = contactsCache.find(c => c.username === currentContact.username);
      if (updated) { currentContact.online = updated.online; currentContact.last_active = updated.last_active; renderChatStatus(currentContact); }
    }
  }
</script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(PAGE)


if __name__ == '__main__':
    print("Чат запущен! Открой в браузере: http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
