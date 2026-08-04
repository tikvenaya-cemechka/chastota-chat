import sqlite3
import random
import string
from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'friends-chat-secret'
socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=20_000_000)

DB_PATH = 'chastota.db'

# Юзернеймы "первых разработчиков" — впиши сюда свой и друзей, когда будете готовы.
# Пример: FOUNDER_USERNAMES = ["oleg", "alina", "alexey"]
FOUNDER_USERNAMES = []


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        name TEXT,
        surname TEXT,
        password_hash TEXT,
        bio TEXT,
        avatar TEXT DEFAULT '😀',
        avatar_photo TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user TEXT,
        to_user TEXT,
        text TEXT,
        time TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS aliases (
        owner TEXT,
        contact TEXT,
        alias TEXT,
        PRIMARY KEY (owner, contact)
    )''')
    # На случай если базa уже существует со старой структурой — добавляем колонку, если её нет
    try:
        conn.execute('ALTER TABLE users ADD COLUMN avatar_photo TEXT')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


init_db()

# pending registrations awaiting captcha, keyed by socket id
pending = {}


def conv_key(a, b):
    return tuple(sorted([a, b]))


def get_user(username):
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_alias(owner, contact):
    conn = get_db()
    row = conn.execute('SELECT alias FROM aliases WHERE owner=? AND contact=?', (owner, contact)).fetchone()
    conn.close()
    return row['alias'] if row else None


def get_contacts(username):
    conn = get_db()
    rows = conn.execute('''
        SELECT DISTINCT CASE WHEN from_user = ? THEN to_user ELSE from_user END AS other
        FROM messages WHERE from_user = ? OR to_user = ?
    ''', (username, username, username)).fetchall()
    conn.close()
    contacts = []
    for r in rows:
        u = get_user(r['other'])
        if u:
            alias = get_alias(username, u['username'])
            contacts.append({
                'username': u['username'],
                'name': alias if alias else u['name'],
                'avatar': u['avatar'],
                'avatar_photo': u.get('avatar_photo'),
                'official': is_official(u['username'])
            })
    return contacts


def is_official(username):
    return username.lower() in [f.lower() for f in FOUNDER_USERNAMES]


def public_user(u):
    return {
        'username': u['username'], 'name': u['name'], 'avatar': u['avatar'],
        'avatar_photo': u.get('avatar_photo'),
        'bio': u.get('bio') or '', 'official': is_official(u['username'])
    }


PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Частота — свой канал связи</title>
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
  .tagline { font-family: 'IBM Plex Mono', monospace; color: var(--text-dim); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; margin-top: -10px; }
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
  .captcha-code {
    font-family: 'IBM Plex Mono', monospace; font-size: 34px; letter-spacing: 10px;
    background: var(--panel-raised); padding: 14px 24px; border-radius: 12px; color: var(--accent);
  }

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
  .empty-hint { padding: 20px; color: var(--text-dim); font-size: 13.5px; text-align: center; }

  .back-btn { background: none; border: none; color: var(--text); font-size: 20px; cursor: pointer; padding: 0 8px 0 0; }
  .chat-header-info { display: flex; align-items: center; gap: 10px; }
  #messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 4px; }
  .msg { max-width: 78%; animation: rise 0.18s ease-out; padding: 2px 0; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
  .msg .meta { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--text-dim); margin-bottom: 3px; }
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

<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
  const socket = io();
  let me = null;
  let currentContact = null;
  let contactsCache = [];

  // --- Тема (тёмная/светлая) ---
  function applyTheme(theme) {
    document.body.classList.toggle('light', theme === 'light');
    const btn = document.getElementById('themeBtn');
    if (btn) btn.textContent = theme === 'light' ? '☀️' : '🌙';
  }
  const savedTheme = localStorage.getItem('chastota_theme') || 'dark';
  applyTheme(savedTheme);
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

  // --- Register ---
  document.getElementById('regBtn').addEventListener('click', () => {
    const name = document.getElementById('regName').value.trim();
    const surname = document.getElementById('regSurname').value.trim();
    const username = document.getElementById('regUsername').value.trim().replace('@', '');
    const password = document.getElementById('regPassword').value;
    document.getElementById('regError').textContent = '';
    if (!name || !username || !password) {
      document.getElementById('regError').textContent = 'Заполни имя, юзернейм и пароль';
      return;
    }
    socket.emit('start_register', { name, surname, username, password });
  });
  socket.on('register_error', d => { document.getElementById('regError').textContent = d.message; });

  socket.on('show_captcha', d => {
    document.getElementById('captchaCode').textContent = d.code;
    document.getElementById('captchaInput').value = '';
    document.getElementById('captchaError').textContent = '';
    showScreen('captchaScreen');
  });
  document.getElementById('captchaBtn').addEventListener('click', () => {
    socket.emit('confirm_captcha', { code: document.getElementById('captchaInput').value.trim() });
  });
  socket.on('captcha_error', d => { document.getElementById('captchaError').textContent = d.message; });

  document.getElementById('toLoginBtn').addEventListener('click', () => showScreen('loginScreen'));
  document.getElementById('toRegisterBtn').addEventListener('click', () => showScreen('registerScreen'));

  // --- Login ---
  document.getElementById('loginBtn').addEventListener('click', () => {
    const username = document.getElementById('loginUsername').value.trim().replace('@', '');
    const password = document.getElementById('loginPassword').value;
    socket.emit('login', { username, password });
  });
  socket.on('login_error', d => { document.getElementById('loginError').textContent = d.message; });

  // --- Auth success (register or login) ---
  socket.on('auth_success', d => {
    me = d.user;
    contactsCache = d.contacts;
    renderContacts(contactsCache);
    showScreen('dashScreen');
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
      item.innerHTML = '<div class="avatar-box">' + avatarHtml(c) + '</div><div><div class="contact-name">' + escapeHtml(c.name) + officialBadge(c.official) + '</div><div class="contact-username">@' + escapeHtml(c.username) + '</div></div>';
      item.addEventListener('click', () => openChat(c));
      list.appendChild(item);
    });
  }

  // --- Search ---
  document.getElementById('searchBtn').addEventListener('click', () => {
    const username = document.getElementById('searchInput').value.trim().replace('@', '');
    if (!username) return;
    if (username === me.username) {
      document.getElementById('searchError').textContent = 'Это твой собственный юзернейм';
      return;
    }
    socket.emit('find_user', { my_username: me.username, username });
  });
  socket.on('find_result', d => {
    const errEl = document.getElementById('searchError');
    if (d.found) { errEl.textContent = ''; openChat(d.user); }
    else { errEl.textContent = 'Юзернейм не найден'; }
  });

  // --- Bio ---
  document.getElementById('bioBtn').addEventListener('click', () => {
    document.getElementById('bioInput').value = me.bio || '';
    document.getElementById('avatarPreview').innerHTML = avatarHtml(me);
    showScreen('bioScreen');
  });
  document.getElementById('bioBackBtn').addEventListener('click', () => showScreen('dashScreen'));
  document.getElementById('bioSaveBtn').addEventListener('click', () => {
    const bio = document.getElementById('bioInput').value.trim();
    socket.emit('update_bio', { username: me.username, bio });
  });
  socket.on('bio_updated', d => { me.bio = d.bio; showScreen('dashScreen'); });

  document.getElementById('avatarFileInput').addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    resizeImage(file, 300).then(dataUrl => {
      socket.emit('update_avatar', { username: me.username, avatar_photo: dataUrl });
    }).catch(() => alert('Не получилось загрузить фото, попробуй другое'));
  });
  document.getElementById('avatarRemoveBtn').addEventListener('click', () => {
    socket.emit('update_avatar', { username: me.username, avatar_photo: null });
  });
  socket.on('avatar_updated', d => {
    me.avatar_photo = d.avatar_photo;
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

  // --- Logout ---
  document.getElementById('logoutBtn').addEventListener('click', () => {
    me = null; currentContact = null;
    document.getElementById('loginUsername').value = '';
    document.getElementById('loginPassword').value = '';
    showScreen('loginScreen');
  });

  // --- Chat ---
  function openChat(contact) {
    currentContact = contact;
    if (!contactsCache.find(c => c.username === contact.username)) {
      contactsCache.unshift(contact);
    }
    document.getElementById('chatAvatar').innerHTML = avatarHtml(contact);
    document.getElementById('chatName').innerHTML = escapeHtml(contact.name) + officialBadge(contact.official);
    document.getElementById('chatUsername').textContent = '@' + contact.username;
    document.getElementById('messages').innerHTML = '';
    socket.emit('open_chat', { my_username: me.username, with_username: contact.username });
    showScreen('chatScreen');
  }
  document.getElementById('backBtn').addEventListener('click', () => showScreen('dashScreen'));

  document.getElementById('renameBtn').addEventListener('click', () => {
    if (!currentContact) return;
    const newName = prompt('Как назвать этот контакт (видно только тебе):', currentContact.name);
    if (newName === null) return;
    socket.emit('set_alias', { owner: me.username, contact: currentContact.username, alias: newName.trim() });
  });
  socket.on('alias_updated', d => {
    if (currentContact && currentContact.username === d.contact) {
      currentContact.name = d.name;
      document.getElementById('chatName').innerHTML = escapeHtml(currentContact.name) + officialBadge(currentContact.official);
    }
    const idx = contactsCache.findIndex(c => c.username === d.contact);
    if (idx !== -1) { contactsCache[idx].name = d.name; }
  });

  socket.on('chat_history', msgs => msgs.forEach(renderMessage));

  function renderMessage(msg) {
    const div = document.createElement('div');
    const isOwn = msg.from_user === me.username;
    div.className = 'msg' + (isOwn ? ' own' : '');
    div.innerHTML = '<div class="meta">' + msg.time + '</div><div class="bubble">' + escapeHtml(msg.text) + '</div>';
    document.getElementById('messages').appendChild(div);
    document.getElementById('messages').scrollTop = document.getElementById('messages').scrollHeight;
  }
  function send() {
    const text = document.getElementById('textInput').value.trim();
    if (!text || !currentContact) return;
    socket.emit('send_message', { from_user: me.username, to_user: currentContact.username, text });
    document.getElementById('textInput').value = '';
  }
  document.getElementById('sendBtn').addEventListener('click', send);
  document.getElementById('textInput').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });

  socket.on('new_message', data => {
    if (currentContact && (data.from_user === currentContact.username || data.from_user === me.username) &&
        (data.to_user === currentContact.username || data.to_user === me.username)) {
      renderMessage(data);
    }
  });

  // Живое обновление списка переписок — без перезахода
  socket.on('contact_update', contact => {
    const idx = contactsCache.findIndex(c => c.username === contact.username);
    if (idx === -1) contactsCache.unshift(contact);
    else contactsCache[idx] = contact;
    if (document.getElementById('dashScreen').classList.contains('active')) {
      renderContacts(contactsCache);
    }
  });
</script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(PAGE)


@socketio.on('start_register')
def handle_start_register(data):
    from flask import request
    username = data.get('username', '').strip().lstrip('@')
    name = data.get('name', '').strip()
    surname = data.get('surname', '').strip()
    password = data.get('password', '')

    if not username or not name or not password:
        emit('register_error', {'message': 'Заполни имя, юзернейм и пароль'})
        return
    if get_user(username):
        emit('register_error', {'message': 'Этот юзернейм уже занят'})
        return

    code = ''.join(random.choices(string.digits, k=5))
    pending[request.sid] = {
        'username': username, 'name': name, 'surname': surname,
        'password': password, 'code': code
    }
    emit('show_captcha', {'code': code})


@socketio.on('confirm_captcha')
def handle_confirm_captcha(data):
    from flask import request
    entered = data.get('code', '').strip()
    reg = pending.get(request.sid)
    if not reg:
        emit('register_error', {'message': 'Сессия истекла, попробуй заново'})
        return
    if entered != reg['code']:
        emit('captcha_error', {'message': 'Код неверный, попробуй ещё раз'})
        return

    conn = get_db()
    conn.execute(
        'INSERT INTO users (username, name, surname, password_hash, bio, avatar) VALUES (?,?,?,?,?,?)',
        (reg['username'], reg['name'], reg['surname'], generate_password_hash(reg['password']), '', '😀')
    )
    conn.commit()
    conn.close()
    del pending[request.sid]

    u = get_user(reg['username'])
    from flask_socketio import join_room
    join_room(u['username'])
    emit('auth_success', {'user': public_user(u), 'contacts': []})


@socketio.on('login')
def handle_login(data):
    username = data.get('username', '').strip().lstrip('@')
    password = data.get('password', '')
    u = get_user(username)
    if not u or not check_password_hash(u['password_hash'], password):
        emit('login_error', {'message': 'Неверный юзернейм или пароль'})
        return
    from flask_socketio import join_room
    join_room(u['username'])
    emit('auth_success', {'user': public_user(u), 'contacts': get_contacts(username)})


@socketio.on('find_user')
def handle_find_user(data):
    my_username = data.get('my_username', '')
    username = data.get('username', '').strip().lstrip('@')
    u = get_user(username)
    if u:
        pu = public_user(u)
        alias = get_alias(my_username, username)
        if alias:
            pu['name'] = alias
        emit('find_result', {'found': True, 'user': pu})
    else:
        emit('find_result', {'found': False})


@socketio.on('open_chat')
def handle_open_chat(data):
    my_username = data.get('my_username')
    with_username = data.get('with_username')
    conn = get_db()
    rows = conn.execute('''
        SELECT from_user, to_user, text, time FROM messages
        WHERE (from_user=? AND to_user=?) OR (from_user=? AND to_user=?)
        ORDER BY id ASC
    ''', (my_username, with_username, with_username, my_username)).fetchall()
    conn.close()
    emit('chat_history', [dict(r) for r in rows])


@socketio.on('send_message')
def handle_send_message(data):
    from_user = data.get('from_user')
    to_user = data.get('to_user')
    text = data.get('text', '')
    recipient = get_user(to_user)
    sender = get_user(from_user)
    if not recipient or not sender:
        return
    time_str = datetime.now().strftime('%H:%M')
    conn = get_db()
    conn.execute('INSERT INTO messages (from_user, to_user, text, time) VALUES (?,?,?,?)',
                 (from_user, to_user, text, time_str))
    conn.commit()
    conn.close()
    msg = {'from_user': from_user, 'to_user': to_user, 'text': text, 'time': time_str}
    emit('new_message', msg, room=from_user)
    emit('new_message', msg, room=to_user)
    # чтобы новая переписка сразу появилась в списке у обоих, без перезахода
    emit('contact_update', public_user(recipient), room=from_user)
    emit('contact_update', public_user(sender), room=to_user)


@socketio.on('update_bio')
def handle_update_bio(data):
    username = data.get('username')
    bio = data.get('bio', '')
    conn = get_db()
    conn.execute('UPDATE users SET bio=? WHERE username=?', (bio, username))
    conn.commit()
    conn.close()
    emit('bio_updated', {'bio': bio})


@socketio.on('update_avatar')
def handle_update_avatar(data):
    username = data.get('username')
    avatar_photo = data.get('avatar_photo') # base64 data-URL или None, если убираем фото
    conn = get_db()
    conn.execute('UPDATE users SET avatar_photo=? WHERE username=?', (avatar_photo, username))
    conn.commit()
    conn.close()
    emit('avatar_updated', {'avatar_photo': avatar_photo})


@socketio.on('set_alias')
def handle_set_alias(data):
    owner = data.get('owner')
    contact = data.get('contact')
    alias = data.get('alias', '').strip()
    conn = get_db()
    if alias:
        conn.execute('INSERT OR REPLACE INTO aliases (owner, contact, alias) VALUES (?,?,?)',
                     (owner, contact, alias))
    else:
        conn.execute('DELETE FROM aliases WHERE owner=? AND contact=?', (owner, contact))
    conn.commit()
    conn.close()
    u = get_user(contact)
    display_name = alias if alias else (u['name'] if u else contact)
    emit('alias_updated', {'contact': contact, 'name': display_name})


if __name__ == '__main__':
    print("Чат запущен! Открой в браузере: http://localhost:5000")
    print("Друзья в той же сети (wifi) могут зайти по твоему локальному IP")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
