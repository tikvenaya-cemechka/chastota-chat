import sqlite3
import random
import string
import re
import time
import secrets
import os
from flask import Flask, render_template_string, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta


def now_iso():
    """UTC-время с суффиксом 'Z' — чтобы браузер каждого пользователя сам корректно
    переводил его в свой локальный часовой пояс, а не показывал время сервера как есть."""
    return datetime.utcnow().isoformat() + 'Z'


def parse_iso(s):
    """Разбирает временную метку, созданную now_iso() (могла быть и без 'Z' — старые записи)."""
    return datetime.fromisoformat(s[:-1] if s.endswith('Z') else s)

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'chastota.db')  # абсолютный путь — чтобы файл базы всегда был один и тот же
ONLINE_SECONDS = 20  # если человек делал запрос за последние N секунд — считаем "в сети"
TYPING_SECONDS = 3

FOUNDER_USERNAMES = []  # впиши сюда свой юзернейм и юзернеймы друзей, когда будете готовы


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
    conn.execute('''CREATE TABLE IF NOT EXISTS pinned_contacts (
        owner TEXT, contact TEXT, PRIMARY KEY (owner, contact)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS blocked_users (
        blocker TEXT, blocked TEXT, PRIMARY KEY (blocker, blocked)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS secret_chats (
        owner TEXT, contact TEXT, password_hash TEXT,
        disguise_name TEXT, disguise_avatar TEXT,
        PRIMARY KEY (owner, contact)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS avatar_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, data TEXT, created_at TEXT
    )''')
    # На случай если база уже существует со старой структурой — добавляем недостающие колонки
    for stmt in [
        'ALTER TABLE users ADD COLUMN avatar_photo TEXT',
        'ALTER TABLE users ADD COLUMN last_active TEXT',
        'ALTER TABLE users ADD COLUMN birthday TEXT',
        "ALTER TABLE users ADD COLUMN privacy_online TEXT DEFAULT 'all'",
        'ALTER TABLE users ADD COLUMN hide_forward_link INTEGER DEFAULT 0',
        'ALTER TABLE messages ADD COLUMN edited INTEGER DEFAULT 0',
        'ALTER TABLE messages ADD COLUMN deleted INTEGER DEFAULT 0',
        "ALTER TABLE messages ADD COLUMN deleted_for TEXT DEFAULT ''",
        'ALTER TABLE messages ADD COLUMN updated_at TEXT',
        'ALTER TABLE messages ADD COLUMN attachment_type TEXT',
        'ALTER TABLE messages ADD COLUMN attachment_data TEXT',
        'ALTER TABLE messages ADD COLUMN attachment_duration INTEGER',
        'ALTER TABLE messages ADD COLUMN reply_to_id INTEGER',
        'ALTER TABLE messages ADD COLUMN forwarded_from TEXT',
        'ALTER TABLE messages ADD COLUMN ttl_seconds INTEGER',
        'ALTER TABLE messages ADD COLUMN expire_at TEXT',
        'ALTER TABLE messages ADD COLUMN secret INTEGER DEFAULT 0',
        'ALTER TABLE messages ADD COLUMN forwarded_from_name TEXT',
        'ALTER TABLE messages ADD COLUMN forwarded_from_hidden INTEGER DEFAULT 0',
        'ALTER TABLE messages ADD COLUMN attachment_meta TEXT',
    ]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    # у старых сообщений updated_at пустой — заполняем временем отправки
    conn.execute("UPDATE messages SET updated_at = time WHERE updated_at IS NULL")
    conn.commit()
    conn.close()


init_db()

pending_regs = {}     # reg_id -> {name, surname, username, password, code, expires}
last_typing = {}      # (from_user, to_user) -> timestamp


# ---------- helpers ----------

def get_user(username):
    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def touch_active(username):
    conn = get_db()
    conn.execute('UPDATE users SET last_active=? WHERE username=?', (now_iso(), username))
    conn.commit()
    conn.close()


def is_official(username):
    return username.lower() in [f.lower() for f in FOUNDER_USERNAMES]


def get_status(u):
    if not u.get('last_active'):
        return {'online': False, 'last_active': None}
    delta = (datetime.utcnow() - parse_iso(u['last_active'])).total_seconds()
    return {'online': delta < ONLINE_SECONDS, 'last_active': u['last_active']}


def get_alias(owner, contact):
    conn = get_db()
    row = conn.execute('SELECT alias FROM aliases WHERE owner=? AND contact=?', (owner, contact)).fetchone()
    conn.close()
    return row['alias'] if row else None


def is_pinned(owner, contact):
    conn = get_db()
    row = conn.execute('SELECT 1 FROM pinned_contacts WHERE owner=? AND contact=?', (owner, contact)).fetchone()
    conn.close()
    return bool(row)


def is_blocked_by(blocker, blocked):
    conn = get_db()
    row = conn.execute('SELECT 1 FROM blocked_users WHERE blocker=? AND blocked=?', (blocker, blocked)).fetchone()
    conn.close()
    return bool(row)


def get_secret_settings(owner, contact):
    conn = get_db()
    row = conn.execute('SELECT * FROM secret_chats WHERE owner=? AND contact=?', (owner, contact)).fetchone()
    conn.close()
    return dict(row) if row else None


def is_secret_pair(user_a, user_b):
    conn = get_db()
    row = conn.execute('''SELECT 1 FROM secret_chats WHERE (owner=? AND contact=?) OR (owner=? AND contact=?)''',
                        (user_a, user_b, user_b, user_a)).fetchone()
    conn.close()
    return bool(row)


def have_exchanged_messages(user_a, user_b):
    conn = get_db()
    row = conn.execute('''SELECT 1 FROM messages WHERE (from_user=? AND to_user=?) OR (from_user=? AND to_user=?) LIMIT 1''',
                        (user_a, user_b, user_b, user_a)).fetchone()
    conn.close()
    return bool(row)


def public_user(u, viewer=None):
    status = get_status(u)
    name = u['name']
    avatar = u['avatar']
    blocked_by_me = False
    pinned = False
    is_secret = False
    has_password = False
    secret = None
    if viewer:
        alias = get_alias(viewer, u['username'])
        if alias:
            name = alias
        blocked_by_me = is_blocked_by(viewer, u['username'])
        pinned = is_pinned(viewer, u['username'])
        secret = get_secret_settings(viewer, u['username'])
        if secret:
            is_secret = True
            has_password = bool(secret.get('password_hash'))
            if secret.get('disguise_name'):
                name = secret['disguise_name']
            if secret.get('disguise_avatar'):
                avatar = secret['disguise_avatar']
    avatar_photo = u.get('avatar_photo') if not blocked_by_me else None
    if secret and secret.get('disguise_avatar'):
        avatar_photo = None
    # приватность онлайн-статуса
    online = status['online']
    last_active = status['last_active']
    if blocked_by_me:
        online = False
    elif viewer and viewer != u['username']:
        privacy = u.get('privacy_online') or 'all'
        allowed = (privacy == 'all') or (privacy == 'contacts' and have_exchanged_messages(viewer, u['username']))
        if not allowed:
            online = False
            last_active = None
    return {
        'username': u['username'], 'name': name,
        'avatar': avatar if not blocked_by_me else None,
        'avatar_photo': avatar_photo,
        'bio': u.get('bio') or '',
        'birthday': u.get('birthday'),
        'official': is_official(u['username']),
        'online': online,
        'last_active': last_active,
        'blocked_by_me': blocked_by_me,
        'pinned': pinned,
        'is_secret': is_secret,
        'has_password': has_password,
        'privacy_online': u.get('privacy_online') or 'all',
        'hide_forward_link': bool(u.get('hide_forward_link')),
    }


def get_contacts(username):
    conn = get_db()
    rows = conn.execute('''
        SELECT from_user, to_user, time, read, deleted, deleted_for, text, attachment_type, secret FROM messages
        WHERE from_user=? OR to_user=? ORDER BY time ASC
    ''', (username, username)).fetchall()
    conn.close()
    last_time = {}       # для какие контакты вообще показывать + сортировка — учитывает и секретные сообщения
    last_preview = {}    # а вот превью текста — только из НЕсекретных, чтобы не спалить содержимое в общем списке
    unread = {}
    for r in rows:
        deleted_for = [x for x in (r['deleted_for'] or '').split(',') if x]
        if r['deleted'] or username in deleted_for:
            continue
        other = r['to_user'] if r['from_user'] == username else r['from_user']
        last_time[other] = r['time']  # строки отсортированы по времени по возрастанию — последняя запись побеждает
        if r['secret']:
            continue
        if r['attachment_type'] == 'photo':
            preview = '📷 Фото'
        elif r['attachment_type'] == 'voice':
            preview = '🎤 Голосовое сообщение'
        elif r['attachment_type'] == 'file':
            preview = '📄 Файл'
        elif r['attachment_type'] == 'location':
            preview = '📍 Геопозиция'
        else:
            preview = r['text'] or ''
        prefix = 'Вы: ' if r['from_user'] == username else ''
        last_preview[other] = prefix + preview
        if r['from_user'] == other and r['to_user'] == username and not r['read']:
            unread[other] = unread.get(other, 0) + 1
    contacts = []
    for other, t in last_time.items():
        u = get_user(other)
        if u:
            c = public_user(u, viewer=username)
            c['last_time'] = t
            c['last_preview'] = last_preview.get(other, '🔒 Секретная переписка')
            c['unread'] = unread.get(other, 0)
            contacts.append(c)
    contacts.sort(key=lambda c: c['last_time'] or '', reverse=True)
    contacts.sort(key=lambda c: 0 if c['pinned'] else 1)  # закреплённые наверх (сортировка стабильная)
    return contacts


def burn_expired_messages(user_a, user_b):
    """Помечает как удалённые сообщения с истёкшим таймером самоуничтожения (проверяется лениво,
    при каждом опросе/открытии чата — не идеально мгновенно, но без фонового воркера иначе никак)."""
    now = now_iso()
    conn = get_db()
    conn.execute('''UPDATE messages SET deleted=1, updated_at=?, attachment_data=NULL, attachment_meta=NULL
        WHERE expire_at IS NOT NULL AND expire_at < ? AND deleted=0
              AND ((from_user=? AND to_user=?) OR (from_user=? AND to_user=?))''',
        (now, now, user_a, user_b, user_b, user_a))
    conn.commit()
    conn.close()


def visible_message(row, viewer):
    """Приводит строку сообщения к виду для конкретного viewer'а.
    Возвращает None, если сообщение для него удалено ("у себя")."""
    m = dict(row)
    deleted_for = (m.pop('deleted_for', '') or '')
    if viewer in [x for x in deleted_for.split(',') if x]:
        return None
    if m.get('deleted'):
        m['text'] = ''
        m['attachment_type'] = None
        m['attachment_data'] = None
        m['attachment_meta'] = None
    return m


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
         '', '😀', now_iso())
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


@app.route('/api/delete_account', methods=['POST'])
def api_delete_account():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    password = (request.json or {}).get('password', '')
    if not check_password_hash(me['password_hash'], password):
        return jsonify({'error': 'Неверный пароль'}), 400
    username = me['username']
    conn = get_db()
    conn.execute('DELETE FROM users WHERE username=?', (username,))
    conn.execute('DELETE FROM messages WHERE from_user=? OR to_user=?', (username, username))
    conn.execute('DELETE FROM aliases WHERE owner=? OR contact=?', (username, username))
    conn.execute('DELETE FROM sessions WHERE username=?', (username,))
    conn.execute('DELETE FROM pinned_contacts WHERE owner=? OR contact=?', (username, username))
    conn.execute('DELETE FROM blocked_users WHERE blocker=? OR blocked=?', (username, username))
    conn.execute('DELETE FROM secret_chats WHERE owner=? OR contact=?', (username, username))
    conn.execute('DELETE FROM avatar_photos WHERE username=?', (username,))
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
    has_attachment = bool(data.get('attachment_data')) or bool(data.get('attachment_meta'))
    if not get_user(to_user) or (not text.strip() and not has_attachment):
        return jsonify({'error': 'bad request'}), 400
    time_str = now_iso()
    conn = get_db()
    blocked = conn.execute('SELECT 1 FROM blocked_users WHERE blocker=? AND blocked=?',
                            (to_user, me['username'])).fetchone()
    attachment_type = data.get('attachment_type')  # 'photo' | 'voice' | 'file' | 'location' | None
    attachment_data = data.get('attachment_data')  # data:...;base64,... (для photo/voice/file)
    attachment_duration = data.get('attachment_duration')  # секунды, для голосовых
    attachment_meta = data.get('attachment_meta')  # JSON-строка: {name,size} для файла, {lat,lng} для геопозиции
    reply_to_id = data.get('reply_to_id')
    forwarded_from = data.get('forwarded_from')
    forwarded_from_name = None
    forwarded_from_hidden = 0
    if forwarded_from:
        orig_user = get_user(forwarded_from)
        if orig_user:
            forwarded_from_name = orig_user['name']
            forwarded_from_hidden = 1 if orig_user.get('hide_forward_link') else 0
        else:
            forwarded_from = None  # исходный пользователь не найден — не пишем пересылку
    ttl_seconds = data.get('ttl_seconds')  # таймер самоуничтожения (секретные чаты)
    # секретность определяется тем, из какого режима чата реально отправлено сообщение
    # (клиент явно указывает), а не просто фактом существования секретного чата с этим человеком —
    # иначе обычные сообщения тоже помечались бы секретными и не показывались в обычном чате
    is_secret = 1 if data.get('secret') else 0
    if blocked:
        # тихо "принимаем" сообщение — отправитель не узнаёт о блокировке, но получатель его не увидит
        conn.close()
        touch_active(me['username'])
        return jsonify({'id': -1, 'from_user': me['username'], 'to_user': to_user, 'text': text,
                         'time': time_str, 'read': 0, 'edited': 0, 'deleted': 0,
                         'attachment_type': attachment_type, 'attachment_data': attachment_data,
                         'attachment_duration': attachment_duration, 'attachment_meta': attachment_meta,
                         'reply_to_id': reply_to_id,
                         'forwarded_from': forwarded_from, 'forwarded_from_name': forwarded_from_name,
                         'forwarded_from_hidden': forwarded_from_hidden,
                         'ttl_seconds': ttl_seconds, 'secret': is_secret})
    try:
        cur = conn.execute('''INSERT INTO messages
            (from_user, to_user, text, time, updated_at, attachment_type, attachment_data, attachment_duration,
             attachment_meta, reply_to_id, forwarded_from, forwarded_from_name, forwarded_from_hidden, ttl_seconds, secret)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (me['username'], to_user, text, time_str, time_str, attachment_type, attachment_data, attachment_duration,
             attachment_meta, reply_to_id, forwarded_from, forwarded_from_name, forwarded_from_hidden, ttl_seconds, is_secret))
        conn.commit()
    except sqlite3.OperationalError as e:
        conn.close()
        if 'disk' in str(e).lower() or 'full' in str(e).lower():
            return jsonify({'error': 'disk_full'}), 507
        return jsonify({'error': 'db_error'}), 500
    msg_id = cur.lastrowid
    conn.close()
    touch_active(me['username'])
    return jsonify({'id': msg_id, 'from_user': me['username'], 'to_user': to_user, 'text': text,
                     'time': time_str, 'read': 0, 'edited': 0, 'deleted': 0,
                     'attachment_type': attachment_type, 'attachment_data': attachment_data,
                     'attachment_duration': attachment_duration, 'attachment_meta': attachment_meta,
                     'reply_to_id': reply_to_id,
                     'forwarded_from': forwarded_from, 'forwarded_from_name': forwarded_from_name,
                     'forwarded_from_hidden': forwarded_from_hidden,
                     'ttl_seconds': ttl_seconds, 'secret': is_secret})


@app.route('/api/pin_chat', methods=['POST'])
def api_pin_chat():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    contact = data.get('contact', '')
    pin = bool(data.get('pin'))
    conn = get_db()
    if pin:
        conn.execute('INSERT OR IGNORE INTO pinned_contacts (owner, contact) VALUES (?,?)',
                      (me['username'], contact))
    else:
        conn.execute('DELETE FROM pinned_contacts WHERE owner=? AND contact=?', (me['username'], contact))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'pinned': pin})


@app.route('/api/block_user', methods=['POST'])
def api_block_user():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    contact = data.get('contact', '')
    block = bool(data.get('block'))
    conn = get_db()
    if block:
        conn.execute('INSERT OR IGNORE INTO blocked_users (blocker, blocked) VALUES (?,?)',
                      (me['username'], contact))
    else:
        conn.execute('DELETE FROM blocked_users WHERE blocker=? AND blocked=?', (me['username'], contact))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'blocked': block})


@app.route('/api/delete_chat', methods=['POST'])
def api_delete_chat():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    contact = data.get('contact', '')
    everyone = bool(data.get('everyone'))
    want_secret = 1 if data.get('secret') else 0
    now = now_iso()
    conn = get_db()
    rows = conn.execute('''
        SELECT id, deleted_for FROM messages
        WHERE ((from_user=? AND to_user=?) OR (from_user=? AND to_user=?)) AND secret=?
    ''', (me['username'], contact, contact, me['username'], want_secret)).fetchall()
    for r in rows:
        if everyone:
            conn.execute('UPDATE messages SET deleted=1, updated_at=?, attachment_data=NULL, attachment_meta=NULL WHERE id=?', (now, r['id']))
        else:
            names = [x for x in (r['deleted_for'] or '').split(',') if x]
            if me['username'] not in names:
                names.append(me['username'])
            conn.execute('UPDATE messages SET deleted_for=?, updated_at=? WHERE id=?',
                         (','.join(names), now, r['id']))
    conn.execute('DELETE FROM pinned_contacts WHERE owner=? AND contact=?', (me['username'], contact))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/clear_history', methods=['POST'])
def api_clear_history():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    contact = data.get('contact', '')
    everyone = bool(data.get('everyone'))
    want_secret = 1 if data.get('secret') else 0
    now = now_iso()
    conn = get_db()
    rows = conn.execute('''
        SELECT id, deleted_for FROM messages
        WHERE ((from_user=? AND to_user=?) OR (from_user=? AND to_user=?)) AND secret=?
    ''', (me['username'], contact, contact, me['username'], want_secret)).fetchall()
    for r in rows:
        if everyone:
            conn.execute('UPDATE messages SET deleted=1, updated_at=?, attachment_data=NULL, attachment_meta=NULL WHERE id=?', (now, r['id']))
        else:
            names = [x for x in (r['deleted_for'] or '').split(',') if x]
            if me['username'] not in names:
                names.append(me['username'])
            conn.execute('UPDATE messages SET deleted_for=?, updated_at=? WHERE id=?',
                         (','.join(names), now, r['id']))
    # в отличие от удаления чата — закреп не трогаем
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/set_secret_chat', methods=['POST'])
def api_set_secret_chat():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    contact = data.get('contact', '').strip()
    if not get_user(contact):
        return jsonify({'error': 'user not found'}), 404
    password = data.get('password')
    disguise_name = data.get('disguise_name')
    disguise_avatar = data.get('disguise_avatar')
    conn = get_db()
    existing = conn.execute('SELECT * FROM secret_chats WHERE owner=? AND contact=?',
                             (me['username'], contact)).fetchone()
    password_hash = existing['password_hash'] if existing else None
    if password:
        password_hash = generate_password_hash(password)
    elif password == '':  # явно очищаем пароль
        password_hash = None
    conn.execute('''INSERT INTO secret_chats (owner, contact, password_hash, disguise_name, disguise_avatar)
        VALUES (?,?,?,?,?)
        ON CONFLICT(owner, contact) DO UPDATE SET
            password_hash=excluded.password_hash,
            disguise_name=COALESCE(?, disguise_name),
            disguise_avatar=COALESCE(?, disguise_avatar)''',
        (me['username'], contact, password_hash, disguise_name, disguise_avatar, disguise_name, disguise_avatar))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/unset_secret_chat', methods=['POST'])
def api_unset_secret_chat():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    contact = data.get('contact', '')
    conn = get_db()
    conn.execute('DELETE FROM secret_chats WHERE owner=? AND contact=?', (me['username'], contact))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/unlock_secret_chat', methods=['POST'])
def api_unlock_secret_chat():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    contact = data.get('contact', '')
    password = data.get('password', '')
    secret = get_secret_settings(me['username'], contact)
    if not secret or not secret.get('password_hash'):
        return jsonify({'ok': True})  # пароль не установлен — открываем без проверки
    if check_password_hash(secret['password_hash'], password):
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'wrong password'}), 403
def api_edit_message():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    msg_id = data.get('id')
    new_text = (data.get('text') or '').strip()
    if not msg_id or not new_text:
        return jsonify({'error': 'bad request'}), 400
    conn = get_db()
    row = conn.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not row or row['from_user'] != me['username']:
        conn.close()
        return jsonify({'error': 'forbidden'}), 403
    now = now_iso()
    conn.execute('UPDATE messages SET text=?, edited=1, updated_at=? WHERE id=?', (new_text, now, msg_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/storage_usage')
def api_storage_usage():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    conn = get_db()
    row = conn.execute('''SELECT
        COALESCE(SUM(LENGTH(attachment_data)),0) AS a,
        COALESCE(SUM(LENGTH(attachment_meta)),0) AS b
        FROM messages WHERE deleted=0''').fetchone()
    conn.close()
    # base64 раздувает данные примерно на треть — грубая оценка реальных байт
    approx_bytes = int((row['a'] + row['b']) * 0.75)
    return jsonify({'approx_bytes': approx_bytes, 'assumed_quota_bytes': 512 * 1024 * 1024})


@app.route('/api/list_attachments')
def api_list_attachments():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    conn = get_db()
    rows = conn.execute('''SELECT id, from_user, to_user, attachment_type, attachment_meta, time,
        LENGTH(attachment_data) AS data_len
        FROM messages
        WHERE deleted=0 AND attachment_type IS NOT NULL AND (from_user=? OR to_user=?)
        ORDER BY data_len DESC LIMIT 200''', (me['username'], me['username'])).fetchall()
    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        d['other'] = d['to_user'] if d['from_user'] == me['username'] else d['from_user']
        d['approx_bytes'] = int((d.pop('data_len') or 0) * 0.75)
        items.append(d)
    return jsonify({'items': items})


@app.route('/api/bulk_delete_attachments', methods=['POST'])
def api_bulk_delete_attachments():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    ids = (request.json or {}).get('ids', [])
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': 'bad request'}), 400
    now = now_iso()
    conn = get_db()
    freed = 0
    for msg_id in ids:
        row = conn.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
        if not row or (row['from_user'] != me['username'] and row['to_user'] != me['username']):
            continue
        freed += len(row['attachment_data'] or '')
        conn.execute('''UPDATE messages SET deleted=1, updated_at=?,
                        attachment_data=NULL, attachment_meta=NULL WHERE id=?''', (now, msg_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'approx_freed_bytes': int(freed * 0.75)})


@app.route('/api/delete_message', methods=['POST'])
def api_delete_message():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json or {}
    msg_id = data.get('id')
    everyone = bool(data.get('everyone'))
    if not msg_id:
        return jsonify({'error': 'bad request'}), 400
    conn = get_db()
    row = conn.execute('SELECT * FROM messages WHERE id=?', (msg_id,)).fetchone()
    if not row or (row['from_user'] != me['username'] and row['to_user'] != me['username']):
        conn.close()
        return jsonify({'error': 'forbidden'}), 403
    now = now_iso()
    if everyone:
        # можно удалить "у всех" любое сообщение в своём чате, не только своё
        # физически стираем сами байты вложения — иначе место на диске не освобождается
        conn.execute('''UPDATE messages SET deleted=1, updated_at=?,
                        attachment_data=NULL, attachment_meta=NULL WHERE id=?''', (now, msg_id))
    else:
        cur_deleted = row['deleted_for'] or ''
        names = [x for x in cur_deleted.split(',') if x]
        if me['username'] not in names:
            names.append(me['username'])
        conn.execute('UPDATE messages SET deleted_for=?, updated_at=? WHERE id=?',
                     (','.join(names), now, msg_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


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
    since_time = request.args.get('since_time', '')
    with_user = request.args.get('with', '')
    want_secret = 1 if request.args.get('secret') == '1' else 0
    server_now = now_iso()

    conn = get_db()
    rows = conn.execute('''
        SELECT id, from_user, to_user, text, time, read, edited, deleted, deleted_for, updated_at, attachment_type, attachment_data, attachment_duration, reply_to_id, forwarded_from, forwarded_from_name, forwarded_from_hidden, ttl_seconds, expire_at, secret, attachment_meta FROM messages
        WHERE id > ? AND (from_user=? OR to_user=?) ORDER BY id ASC
    ''', (since_id, me['username'], me['username'])).fetchall()
    new_messages = [m for m in (visible_message(r, me['username']) for r in rows) if m]

    # отдельно ловим правки/удаления уже присланных ранее сообщений (id <= since_id)
    updated_messages = []
    if with_user and since_time:
        upd_rows = conn.execute('''
            SELECT id, from_user, to_user, text, time, read, edited, deleted, deleted_for, updated_at, attachment_type, attachment_data, attachment_duration, reply_to_id, forwarded_from, forwarded_from_name, forwarded_from_hidden, ttl_seconds, expire_at, secret, attachment_meta FROM messages
            WHERE id <= ? AND updated_at > ? AND secret=? AND
                  ((from_user=? AND to_user=?) OR (from_user=? AND to_user=?))
            ORDER BY id ASC
        ''', (since_id, since_time, want_secret, me['username'], with_user, with_user, me['username'])).fetchall()
        updated_messages = [m for m in (visible_message(r, me['username']) for r in upd_rows) if m]

    read_up_to_id = 0
    typing = False
    if with_user:
        # для сообщений с таймером — при первом прочтении запускаем обратный отсчёт
        rows_to_arm = conn.execute('''SELECT id, ttl_seconds FROM messages
            WHERE from_user=? AND to_user=? AND read=0 AND secret=? AND ttl_seconds IS NOT NULL AND expire_at IS NULL''',
            (with_user, me['username'], want_secret)).fetchall()
        for row in rows_to_arm:
            exp = (datetime.utcnow() + timedelta(seconds=row['ttl_seconds'])).isoformat() + 'Z'
            conn.execute('UPDATE messages SET expire_at=? WHERE id=?', (exp, row['id']))
        conn.execute('UPDATE messages SET read=1 WHERE from_user=? AND to_user=? AND read=0 AND secret=?',
                     (with_user, me['username'], want_secret))
        conn.commit()
        row = conn.execute('SELECT MAX(id) AS m FROM messages WHERE from_user=? AND to_user=? AND read=1 AND secret=?',
                            (me['username'], with_user, want_secret)).fetchone()
        read_up_to_id = row['m'] or 0
        ts = last_typing.get((with_user, me['username']))
        typing = bool(ts and time.time() - ts < TYPING_SECONDS)
    conn.close()
    if with_user:
        burn_expired_messages(me['username'], with_user)

    max_id = since_id
    if new_messages:
        max_id = max(m['id'] for m in new_messages)

    return jsonify({
        'new_messages': new_messages,
        'updated_messages': updated_messages,
        'contacts': get_contacts(me['username']),
        'read_up_to_id': read_up_to_id,
        'typing': typing,
        'max_id': max_id,
        'sync_time': server_now
    })


@app.route('/api/open_chat')
def api_open_chat():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    with_user = request.args.get('with', '')
    want_secret = 1 if request.args.get('secret') == '1' else 0
    server_now = now_iso()
    conn = get_db()
    rows = conn.execute('''
        SELECT id, from_user, to_user, text, time, read, edited, deleted, deleted_for, updated_at, attachment_type, attachment_data, attachment_duration, reply_to_id, forwarded_from, forwarded_from_name, forwarded_from_hidden, ttl_seconds, expire_at, secret, attachment_meta FROM messages
        WHERE ((from_user=? AND to_user=?) OR (from_user=? AND to_user=?)) AND secret=?
        ORDER BY id ASC
    ''', (me['username'], with_user, with_user, me['username'], want_secret)).fetchall()
    for r in rows:
        if r['from_user'] == with_user and not r['read'] and r['ttl_seconds'] is not None and not r['expire_at']:
            exp = (datetime.utcnow() + timedelta(seconds=r['ttl_seconds'])).isoformat() + 'Z'
            conn.execute('UPDATE messages SET expire_at=? WHERE id=?', (exp, r['id']))
    conn.execute('UPDATE messages SET read=1 WHERE from_user=? AND to_user=? AND read=0 AND secret=?',
                 (with_user, me['username'], want_secret))
    conn.commit()
    conn.close()
    burn_expired_messages(me['username'], with_user)
    conn = get_db()
    rows = conn.execute('''
        SELECT id, from_user, to_user, text, time, read, edited, deleted, deleted_for, updated_at, attachment_type, attachment_data, attachment_duration, reply_to_id, forwarded_from, forwarded_from_name, forwarded_from_hidden, ttl_seconds, expire_at, secret, attachment_meta FROM messages
        WHERE ((from_user=? AND to_user=?) OR (from_user=? AND to_user=?)) AND secret=?
        ORDER BY id ASC
    ''', (me['username'], with_user, with_user, me['username'], want_secret)).fetchall()
    conn.close()
    messages = [m for m in (visible_message(r, me['username']) for r in rows) if m]
    max_id = max([r['id'] for r in rows], default=0)
    return jsonify({'messages': messages, 'max_id': max_id, 'sync_time': server_now})


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


@app.route('/api/update_name', methods=['POST'])
def api_update_name():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    name = (request.json or {}).get('name', '').strip()
    if not name:
        return jsonify({'error': 'Имя не может быть пустым'}), 400
    conn = get_db()
    conn.execute('UPDATE users SET name=? WHERE username=?', (name, me['username']))
    conn.commit()
    conn.close()
    return jsonify({'name': name})


@app.route('/api/change_username', methods=['POST'])
def api_change_username():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    new_username = (request.json or {}).get('username', '').strip().lstrip('@')
    if not re.match(r'^[A-Za-z0-9_]{3,32}$', new_username):
        return jsonify({'error': 'Юзернейм: только латинские буквы, цифры и _, от 3 до 32 символов'}), 400
    old_username = me['username']
    if new_username == old_username:
        return jsonify({'ok': True, 'username': old_username})
    if get_user(new_username):
        return jsonify({'error': 'Этот юзернейм уже занят'}), 400
    conn = get_db()
    conn.execute('UPDATE users SET username=? WHERE username=?', (new_username, old_username))
    conn.execute('UPDATE messages SET from_user=? WHERE from_user=?', (new_username, old_username))
    conn.execute('UPDATE messages SET to_user=? WHERE to_user=?', (new_username, old_username))
    conn.execute('UPDATE aliases SET owner=? WHERE owner=?', (new_username, old_username))
    conn.execute('UPDATE aliases SET contact=? WHERE contact=?', (new_username, old_username))
    conn.execute('UPDATE sessions SET username=? WHERE username=?', (new_username, old_username))
    conn.execute('UPDATE pinned_contacts SET owner=? WHERE owner=?', (new_username, old_username))
    conn.execute('UPDATE pinned_contacts SET contact=? WHERE contact=?', (new_username, old_username))
    conn.execute('UPDATE blocked_users SET blocker=? WHERE blocker=?', (new_username, old_username))
    conn.execute('UPDATE blocked_users SET blocked=? WHERE blocked=?', (new_username, old_username))
    conn.execute('UPDATE secret_chats SET owner=? WHERE owner=?', (new_username, old_username))
    conn.execute('UPDATE secret_chats SET contact=? WHERE contact=?', (new_username, old_username))
    conn.execute('UPDATE avatar_photos SET username=? WHERE username=?', (new_username, old_username))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'username': new_username})


@app.route('/api/update_avatar', methods=['POST'])
def api_update_avatar():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    avatar_photo = (request.json or {}).get('avatar_photo')
    conn = get_db()
    conn.execute('UPDATE users SET avatar_photo=? WHERE username=?', (avatar_photo, me['username']))
    if avatar_photo:
        conn.execute('INSERT INTO avatar_photos (username, data, created_at) VALUES (?,?,?)',
                     (me['username'], avatar_photo, now_iso()))
    conn.commit()
    conn.close()
    return jsonify({'avatar_photo': avatar_photo})


@app.route('/api/update_birthday', methods=['POST'])
def api_update_birthday():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    birthday = (request.json or {}).get('birthday')  # 'YYYY-MM-DD' или null
    conn = get_db()
    conn.execute('UPDATE users SET birthday=? WHERE username=?', (birthday, me['username']))
    conn.commit()
    conn.close()
    return jsonify({'birthday': birthday})


@app.route('/api/update_privacy', methods=['POST'])
def api_update_privacy():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    privacy = (request.json or {}).get('privacy_online', 'all')
    if privacy not in ('all', 'none', 'contacts'):
        return jsonify({'error': 'bad value'}), 400
    conn = get_db()
    conn.execute('UPDATE users SET privacy_online=? WHERE username=?', (privacy, me['username']))
    conn.commit()
    conn.close()
    return jsonify({'privacy_online': privacy})


@app.route('/api/update_forward_privacy', methods=['POST'])
def api_update_forward_privacy():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    hide = bool((request.json or {}).get('hide_forward_link'))
    conn = get_db()
    conn.execute('UPDATE users SET hide_forward_link=? WHERE username=?', (1 if hide else 0, me['username']))
    conn.commit()
    conn.close()
    return jsonify({'hide_forward_link': hide})


@app.route('/api/get_photos')
def api_get_photos():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    username = request.args.get('username', '')
    u = get_user(username)
    if not u:
        return jsonify({'error': 'not found'}), 404
    if is_blocked_by(username, me['username']):
        return jsonify({'photos': []})
    conn = get_db()
    rows = conn.execute('SELECT id, data, created_at FROM avatar_photos WHERE username=? ORDER BY id DESC',
                        (username,)).fetchall()
    conn.close()
    return jsonify({'photos': [dict(r) for r in rows]})


@app.route('/api/delete_photo', methods=['POST'])
def api_delete_photo():
    me = require_auth()
    if not me:
        return jsonify({'error': 'unauthorized'}), 401
    photo_id = (request.json or {}).get('id')
    conn = get_db()
    row = conn.execute('SELECT * FROM avatar_photos WHERE id=? AND username=?',
                       (photo_id, me['username'])).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    conn.execute('DELETE FROM avatar_photos WHERE id=?', (photo_id,))
    # если удалили текущую аватарку — ставим следующую по свежести (или пусто)
    u = get_user(me['username'])
    if u.get('avatar_photo') == row['data']:
        newest = conn.execute('SELECT data FROM avatar_photos WHERE username=? ORDER BY id DESC LIMIT 1',
                              (me['username'],)).fetchone()
        conn.execute('UPDATE users SET avatar_photo=? WHERE username=?',
                    (newest['data'] if newest else None, me['username']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


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
  .splash-logo { font-size: 30px; font-weight: 700; color: var(--accent); letter-spacing: 1px; }
  .splash-fact { font-size: 15px; color: var(--text-dim); max-width: 320px; line-height: 1.55; min-height: 60px; }
  .splash-continue-btn { opacity: 0; pointer-events: none; transition: opacity 0.7s ease; background: var(--accent); color: #1b1204; border: none; border-radius: 10px; padding: 13px 34px; font-weight: 600; font-size: 15px; cursor: pointer; }
  .splash-continue-btn.visible { opacity: 1; pointer-events: auto; }
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
  .search-radio-panel { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 18px 0 6px; }
  .radio-waves { display: flex; gap: 5px; height: 20px; align-items: flex-end; }
  .radio-waves span { width: 4px; background: var(--accent); border-radius: 2px; animation: radio-wave 1s infinite ease-in-out; }
  .radio-waves span:nth-child(1) { height: 8px; animation-delay: 0s; }
  .radio-waves span:nth-child(2) { height: 16px; animation-delay: 0.15s; }
  .radio-waves span:nth-child(3) { height: 8px; animation-delay: 0.3s; }
  @keyframes radio-wave { 0%, 100% { transform: scaleY(0.4); opacity: 0.5; } 50% { transform: scaleY(1); opacity: 1; } }
  .radio-icon { font-size: 34px; position: relative; }
  .radio-icon.not-found::after { content: ''; position: absolute; left: -6px; right: -6px; top: 50%; height: 3px; background: var(--danger); transform: rotate(-25deg); border-radius: 2px; }
  .radio-status { font-size: 13px; color: var(--text-dim); font-family: 'IBM Plex Mono', monospace; }
  #botActionBar { padding: 14px 20px; border-top: 1px solid var(--border); background: var(--panel); }
  #botActionBar button { width: 100%; background: var(--accent); color: #1b1204; border: none; border-radius: 10px; padding: 12px; font-weight: 600; cursor: pointer; }
  .date-separator { text-align: center; font-size: 12px; color: var(--text-dim); background: var(--panel-raised); padding: 4px 12px; border-radius: 10px; margin: 10px auto; width: fit-content; }
  .profile-nav-arrow { position: absolute; top: 50%; transform: translateY(-50%); background: rgba(0,0,0,0.4); color: #fff; border: none; width: 40px; height: 40px; border-radius: 50%; font-size: 22px; cursor: pointer; z-index: 5; }
  .profile-viewer-info { padding: 12px 22px; font-size: 14px; line-height: 1.8; color: var(--text); background: var(--panel); }
  .profile-viewer-info b { color: var(--text-dim); font-weight: 500; font-size: 12.5px; display: inline-block; min-width: 130px; }
  #birthdayBanner { background: rgba(255,193,7,0.12); color: #e0a800; font-size: 13px; padding: 10px 16px; text-align: center; }
  #forwardToolbar { background: var(--panel-raised); padding: 10px 16px; display: flex; align-items: center; gap: 10px; font-size: 13px; }
  #forwardToolbar input { flex: 1; min-width: 0; }
  #forwardToolbar button, #forwardPreviewBar button { background: var(--accent); color: #1b1204; border: none; border-radius: 8px; padding: 6px 12px; font-size: 12px; cursor: pointer; flex-shrink: 0; }
  #forwardCancelBtn, #forwardHideSenderBtn, #forwardPreviewCancel, #forwardPreviewHideSenderBtn { background: var(--panel) !important; color: var(--text-dim) !important; }
  .fwd-select-badge { position: absolute; bottom: -2px; right: -2px; background: var(--accent); color: #1b1204; font-size: 10px; font-weight: 700; border-radius: 50%; width: 18px; height: 18px; display: flex; align-items: center; justify-content: center; border: 2px solid var(--bg); }
  #storageWarningBanner { background: rgba(239,68,68,0.14); color: var(--danger); font-size: 13px; padding: 10px 16px; display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  #storageWarningBanner button { background: var(--danger); color: #fff; border: none; border-radius: 8px; padding: 6px 12px; font-size: 12px; cursor: pointer; flex-shrink: 0; }
  .cleanup-item { display: flex; align-items: center; gap: 10px; padding: 10px 22px; border-bottom: 1px solid var(--border); }
  .cleanup-item .avatar-box { width: 36px; height: 36px; font-size: 16px; }
  .cleanup-item-info { flex: 1; font-size: 13px; }
  .cleanup-item-size { font-size: 11px; color: var(--text-dim); }
  .my-photo-thumb { width: 60px; height: 60px; border-radius: 10px; overflow: hidden; position: relative; }
  .my-photo-thumb img { width: 100%; height: 100%; object-fit: cover; }
  .my-photo-thumb button { position: absolute; top: 2px; right: 2px; background: rgba(0,0,0,0.6); color: #fff; border: none; border-radius: 50%; width: 18px; height: 18px; font-size: 11px; line-height: 1; cursor: pointer; }
  .secret-chat-btn { width: 100%; margin-top: 10px; background: none; border: 1px dashed var(--accent); color: var(--accent); border-radius: 10px; padding: 10px; font-size: 13px; cursor: pointer; }
  #secretBanner { background: rgba(59,167,245,0.12); color: var(--accent); font-size: 12.5px; padding: 10px 16px; text-align: center; border-bottom: 1px solid var(--border); line-height: 1.6; }
  #secretBanner b { display: block; margin-bottom: 4px; }
  #replyBar { display: flex; align-items: center; gap: 10px; padding: 8px 16px; background: var(--panel); border-top: 1px solid var(--border); }
  .reply-bar-content { flex: 1; border-left: 3px solid var(--accent); padding-left: 8px; }
  .reply-bar-label { font-size: 11px; color: var(--accent); display: block; }
  #replyBarText { font-size: 13px; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 260px; }
  #replyBarCancel { background: none; border: none; color: var(--text-dim); font-size: 16px; cursor: pointer; }
  .msg .reply-quote { border-left: 3px solid var(--accent); padding-left: 8px; margin-bottom: 5px; font-size: 12.5px; color: var(--text-dim); opacity: 0.9; }
  .msg .forwarded-tag { font-size: 11px; color: var(--text-dim); font-style: italic; margin-bottom: 3px; }
  .fwd-name-link { font-weight: 600; cursor: pointer; text-decoration: underline; }
  .fwd-name-hidden { font-weight: 600; cursor: pointer; }
  .msg.swiping { transition: none; }
  .search-radio-panel { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 18px 0 6px; }
  .radio-waves { display: flex; gap: 5px; height: 20px; align-items: flex-end; }
  .radio-waves span { width: 4px; background: var(--accent); border-radius: 2px; animation: radio-wave 1s infinite ease-in-out; }
  .radio-waves span:nth-child(1) { height: 8px; animation-delay: 0s; }
  .radio-waves span:nth-child(2) { height: 16px; animation-delay: 0.15s; }
  .radio-waves span:nth-child(3) { height: 8px; animation-delay: 0.3s; }
  @keyframes radio-wave { 0%, 100% { transform: scaleY(0.4); opacity: 0.5; } 50% { transform: scaleY(1); opacity: 1; } }
  #chatSearchBar { display: none; gap: 8px; padding: 10px 16px; background: var(--panel); border-bottom: 1px solid var(--border); align-items: center; }
  #chatSearchBar input { flex: 1; }
  #chatSearchNav { display: none; position: fixed; bottom: 90px; right: 18px; flex-direction: column; align-items: center; gap: 4px; background: var(--panel-raised); border-radius: 12px; padding: 8px 6px; z-index: 30; }
  #chatSearchNav button { background: none; border: none; color: var(--accent); font-size: 18px; cursor: pointer; padding: 4px 8px; }
  #searchMatchCount { font-size: 10px; color: var(--text-dim); }
  .msg.search-highlight .bubble { outline: 2px solid var(--accent); }
  .wallpaper-swatch { width: 44px; height: 44px; border-radius: 10px; cursor: pointer; border: 2px solid var(--border); }
  .timer-wheel { display: flex; align-items: center; justify-content: center; gap: 14px; padding: 10px 22px 4px; }
  .timer-wheel button { width: 38px; height: 38px; border-radius: 50%; border: none; background: var(--panel-raised); color: var(--text); font-size: 18px; cursor: pointer; }
  .timer-wheel .timer-value { font-size: 22px; font-weight: 700; min-width: 60px; text-align: center; }

  .contacts-title { padding: 12px 20px 6px; font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.06em; }
  .contacts-list { flex: 1; overflow-y: auto; padding: 0 10px 10px; }
  .contact-item { display: flex; align-items: center; gap: 12px; padding: 12px; border-radius: 12px; cursor: pointer; transition: background 0.15s; }
  .unread-badge { background: var(--accent); color: #1b1204; font-size: 12px; font-weight: 700; border-radius: 999px; min-width: 22px; height: 22px; padding: 0 6px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .contact-item:hover { background: var(--panel-raised); }
  .avatar-box { width: 42px; height: 42px; border-radius: 12px; background: var(--panel-raised); display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; overflow: hidden; }
  .avatar-box img { width: 100%; height: 100%; object-fit: cover; }
  .avatar-blocked { width: 100%; height: 100%; background: #7a7a7a; }
  .pin-icon { font-size: 12px; margin-left: 4px; }
  .contact-name { font-weight: 600; font-size: 14.5px; display: inline-flex; align-items: center; gap: 4px; }
  .official-badge { display: inline-flex; width: 15px; height: 15px; flex-shrink: 0; }
  .contact-username { font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; color: var(--text-dim); }
  .contact-status { font-family: 'IBM Plex Mono', monospace; font-size: 10.5px; margin-top: 1px; }
  .contact-preview { font-size: 12.5px; color: var(--text-dim); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
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
  .msg .edited-tag { font-size: 10px; color: var(--text-dim); margin-left: 5px; }
  .msg .translate-btn { display: block; margin-top: 4px; font-size: 11px; color: var(--signal); background: none; border: none; cursor: pointer; padding: 0; text-decoration: underline; }
  .msg .translation { margin-top: 5px; padding-top: 5px; border-top: 1px dashed rgba(0,0,0,0.15); font-size: 13.5px; font-style: italic; opacity: 0.9; }
  .msg-menu-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 50; display: flex; align-items: flex-end; justify-content: center; }
  .msg-menu { background: var(--panel); border-radius: 14px 14px 0 0; width: 100%; max-width: 420px; padding: 8px 0 20px; }
  .msg-menu button { display: block; width: 100%; text-align: left; padding: 14px 22px; background: none; border: none; color: var(--text); font-size: 15px; cursor: pointer; }
  .msg-menu button:active { background: var(--panel-raised); }
  .msg-menu button.danger { color: #f16565; }
  .msg-menu .menu-check-row { display: flex; align-items: center; gap: 10px; padding: 10px 22px; }
  #composer { display: flex; gap: 8px; padding: 16px 20px; border-top: 1px solid var(--border); background: var(--panel); align-items: center; }
  .composer-icon-btn { background: var(--panel-raised); border: none; border-radius: 10px; width: 42px; height: 42px; flex-shrink: 0; font-size: 18px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
  .composer-icon-btn.recording { background: #c0392b; color: #fff; animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.6; } }
  .msg .bubble img.msg-photo { max-width: 220px; border-radius: 10px; display: block; margin-top: 4px; cursor: pointer; }
  .voice-msg { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
  .file-msg, .location-msg { display: flex; align-items: center; gap: 10px; text-decoration: none; color: inherit; font-size: 24px; margin-top: 4px; }
  .file-msg .file-name, .location-msg .file-name { font-size: 13.5px; font-weight: 600; }
  .file-msg .file-size, .location-msg .file-size { font-size: 11.5px; color: var(--text-dim); }
  .voice-speed-btn { background: var(--panel-raised); border: none; border-radius: 8px; color: var(--text); font-size: 11px; padding: 3px 7px; cursor: pointer; }
  #undoSnackbar { position: fixed; bottom: 90px; left: 50%; transform: translateX(-50%); background: var(--panel-raised); border-radius: 10px; padding: 10px 16px; display: flex; align-items: center; gap: 14px; font-size: 13px; box-shadow: 0 4px 14px rgba(0,0,0,0.3); z-index: 40; }
  #undoSnackbarBtn { background: none; border: none; color: var(--accent); font-weight: 700; cursor: pointer; font-size: 13px; }
  .draft-label { color: var(--accent) !important; }
  .voice-msg button { background: var(--accent); color: #1b1204; border: none; border-radius: 50%; width: 34px; height: 34px; cursor: pointer; flex-shrink: 0; }
  .voice-msg .voice-duration { font-size: 12px; color: var(--text-dim); }
  #textInput { flex: 1; width: auto; }
  #sendBtn { background: var(--accent); color: #1b1204; border: none; border-radius: 10px; padding: 0 22px; font-weight: 600; cursor: pointer; }

  .bio-box { width: 280px; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; text-align: left; }
  textarea { width: 100%; background: var(--panel-raised); border: 1px solid var(--border); color: var(--text); font-family: 'Inter', sans-serif; font-size: 14px; padding: 10px; border-radius: 8px; resize: vertical; min-height: 60px; }

  #messages::-webkit-scrollbar, .contacts-list::-webkit-scrollbar { width: 6px; }
  #messages::-webkit-scrollbar-thumb, .contacts-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<div id="splashScreen" class="screen center active">
  <div class="splash-logo">📻 Частота</div>
  <div class="splash-fact" id="splashFact"></div>
  <button id="splashContinueBtn" class="splash-continue-btn">Продолжить</button>
</div>
<div id="registerScreen" class="screen center">
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
      <button class="icon-btn" id="supportBtn" title="Техподдержка">🛟</button>
      <button class="icon-btn" id="themeBtn">🌙</button>
      <button class="icon-btn" id="bioBtn">О себе</button>
      <button class="icon-btn" id="logoutBtn">Выйти</button>
    </div>
  </header>
  <div id="birthdayBanner" style="display:none;"></div>
  <div id="storageWarningBanner" style="display:none;"></div>
  <div id="forwardToolbar" style="display:none;">
    <div id="forwardToolbarInfo" style="flex:1;"></div>
    <div id="forwardCaptionWrap" style="display:none; align-items:center; gap:8px;">
      <input type="text" id="forwardCaptionInput" placeholder="Подпись (необязательно)">
      <button id="forwardHideSenderBtn" title="Скрыть имя отправителя">☰</button>
      <button id="forwardSendBtn">Отправить</button>
    </div>
    <button id="forwardCancelBtn">✕</button>
  </div>
  <div class="search-block">
    <div class="search-row">
      <input type="text" id="searchInput" placeholder="Юзернейм собеседника (@nickname)">
      <button id="searchBtn">Найти</button>
    </div>
    <div id="searchRadioPanel" class="search-radio-panel" style="display:none;">
      <div class="radio-waves" id="radioWaves"><span></span><span></span><span></span></div>
      <div class="radio-icon" id="radioIcon">📻</div>
      <div class="radio-status" id="radioStatus">Поиск...</div>
    </div>
    <div id="searchError"></div>
    <div id="searchResult"></div>
  </div>
  <div id="savedChatBtn" class="contact-item" style="margin:0 12px;">
    <div class="avatar-box" style="background:var(--accent); color:#1b1204;">⭐</div>
    <div><div class="contact-name">Избранное</div><div class="contact-username">заметки, ссылки, файлы — видно только тебе</div></div>
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
    <div style="font-size:13px; color:var(--text-dim);">Имя</div>
    <input type="text" id="nameInput" placeholder="Твоё имя" style="width:100%; margin-top:6px; margin-bottom:12px;">
    <div style="font-size:13px; color:var(--text-dim);">Юзернейм</div>
    <div style="display:flex; align-items:center; gap:6px; margin-top:6px; margin-bottom:4px;">
      <span style="color:var(--text-dim);">@</span>
      <input type="text" id="usernameInput" placeholder="username" style="flex:1;">
    </div>
    <div id="usernameError" style="color:var(--danger); font-size:12px; min-height:16px;"></div>
    <textarea id="bioInput" placeholder="Расскажи что-нибудь о себе..."></textarea>
    <div style="margin-top:12px; font-size:13px; color:var(--text-dim);">День рождения</div>
    <input type="date" id="birthdayInput" style="width:100%; margin-top:6px;">
    <div style="display:flex; gap:8px; margin-top:12px;">
      <button class="primary" id="bioSaveBtn" style="flex:1;">Сохранить</button>
      <button class="icon-btn" id="bioBackBtn">Назад</button>
    </div>
    <div style="margin-top:18px; font-size:13px; color:var(--text-dim);">Мои фото</div>
    <div id="myPhotosList" style="display:flex; gap:8px; flex-wrap:wrap; margin-top:8px;"></div>
    <div style="margin-top:18px; font-size:13px; color:var(--text-dim);">Кто видит мой статус "В сети"</div>
    <select id="privacySelect" style="width:100%; margin-top:6px;">
      <option value="all">Все</option>
      <option value="contacts">Только контакты</option>
      <option value="none">Никто</option>
    </select>
    <label style="display:flex; align-items:center; gap:10px; margin-top:16px; font-size:13px;">
      <input type="checkbox" id="hideForwardCheck">
      Скрыть профиль при пересылке моих сообщений (моё имя будет некликабельным)
    </label>
    <button id="deleteAccountBtn" class="danger" style="width:100%; margin-top:24px; background:none; border:1px solid var(--danger); color:var(--danger);">Удалить аккаунт</button>
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
    <button class="icon-btn" id="chatMenuBtn">⋮</button>
  </header>
  <div id="secretBanner" style="display:none;"></div>
  <div id="chatSearchBar">
    <input type="text" id="chatSearchInput" placeholder="Поиск по переписке...">
    <button id="chatSearchCloseBtn">✕</button>
  </div>
  <div id="messages"></div>
  <div id="chatSearchNav"><button id="searchPrevBtn">↑</button><span id="searchMatchCount"></span><button id="searchNextBtn">↓</button></div>
  <div id="replyBar" style="display:none;">
    <div class="reply-bar-content"><span class="reply-bar-label">Ответ</span><div id="replyBarText"></div></div>
    <button id="replyBarCancel">✕</button>
  </div>
  <div id="forwardPreviewBar" style="display:none;">
    <div class="reply-bar-content"><span class="reply-bar-label">Пересылка</span><div id="forwardPreviewText"></div></div>
    <button id="forwardPreviewHideSenderBtn" title="Скрыть имя отправителя">☰</button>
    <button id="forwardPreviewCancel">✕</button>
  </div>
  <div id="composer">
    <input type="file" id="wallpaperFileInput" accept="image/*" style="display:none;">
    <input type="file" id="photoInput" accept="image/*" multiple style="display:none;">
    <input type="file" id="fileInput" style="display:none;">
    <button class="composer-icon-btn" id="attachBtn" title="Прикрепить">📎</button>
    <input type="text" id="textInput" placeholder="Сообщение...">
    <button class="composer-icon-btn" id="voiceBtn" title="Удерживай, чтобы записать голосовое">🎤</button>
    <button id="sendBtn">Отправить</button>
  </div>
  <div id="botActionBar" style="display:none;">
    <button id="botFactBtn">Интересный факт</button>
  </div>
  <div id="undoSnackbar" style="display:none;">
    <span>Сообщение отправлено</span>
    <button id="undoSnackbarBtn">Отменить</button>
  </div>
</div>
<div id="photoPreviewOverlay" class="msg-menu-overlay" style="display:none;"><img id="photoPreviewImg" style="max-width:92%; max-height:85%; border-radius:10px;"></div>
<div id="storageCleanupOverlay" class="msg-menu-overlay" style="display:none; align-items:center; justify-content:center;">
  <div class="msg-menu" style="max-height:80vh; display:flex; flex-direction:column;">
    <div id="storageCleanupTitle" style="padding:16px 22px 6px; font-size:15px; font-weight:600;"></div>
    <div id="storageCleanupSubtitle" style="padding:0 22px 10px; font-size:12.5px; color:var(--text-dim);"></div>
    <div id="storageCleanupList" style="overflow-y:auto; flex:1;"></div>
    <div style="display:flex; gap:8px; padding:14px 22px;">
      <button id="storageCleanupDeleteBtn" class="danger" style="flex:1;" disabled>Удалить выбранные</button>
      <button id="storageCleanupCloseBtn">Закрыть</button>
    </div>
  </div>
</div>
<div id="profileViewerOverlay" class="msg-menu-overlay" style="display:none; flex-direction:column;">
  <div id="profileViewerPhotoArea" style="position:relative; flex:1; display:flex; align-items:center; justify-content:center;">
    <button id="profilePhotoPrev" class="profile-nav-arrow" style="left:10px;">‹</button>
    <img id="profileViewerImg" style="max-width:92%; max-height:70vh; border-radius:10px; object-fit:contain;">
    <button id="profilePhotoNext" class="profile-nav-arrow" style="right:10px;">›</button>
  </div>
  <div id="profileViewerInfo" class="profile-viewer-info"></div>
  <div style="display:flex; gap:10px; padding:14px 20px;">
    <a id="profilePhotoDownload" download="photo.jpg" style="flex:1;"><button style="width:100%;">Скачать фото</button></a>
    <button id="profileViewerClose">Закрыть</button>
  </div>
</div>

<script>
  let me = null;
  let token = localStorage.getItem('chastota_token') || null;
  let currentContact = null;
  let contactsCache = [];
  let sinceId = 0;
  let sinceTime = '';
  let pendingRegId = null;
  let pollTimer = null;
  let messagesById = {}; // id -> msg object (для меню/редактирования/перевода)
  let lastRenderedDateKey = '';

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
    if (user && user.blocked_by_me) return '<div class="avatar-blocked"></div>';
    if (user && user.avatar_photo) return '<img src="' + user.avatar_photo + '">';
    return (user && user.avatar) ? user.avatar : '😀';
  }
  function statusInfo(user) {
    if (user.blocked_by_me) return { text: 'заблокирован(а)', cls: 'status-offline' };
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
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 8000);
    try {
      const res = await fetch(url, Object.assign({ signal: controller.signal }, opts));
      clearTimeout(timeoutId);
      return { ok: res.ok, data: await res.json() };
    } catch (e) {
      clearTimeout(timeoutId);
      return { ok: false, data: { error: 'Нет связи с сервером, попробуй ещё раз' } };
    }
  }

  // --- Факты для экрана загрузки ---
  const FACTS = [
    'А вы знали? Отпечаток носа у каждой кошки так же уникален, как и отпечатки пальцев у человека.',
    'А вы знали? Один день на Венере длиннее, чем один её год.',
    'А вы знали? Осьминоги имеют три сердца и голубую кровь.',
    'А вы знали? Мёд практически не портится — археологи находили съедобный мёд возрастом более 3000 лет.',
    'А вы знали? Свет от Солнца долетает до Земли примерно за 8 минут 20 секунд.',
    'А вы знали? У жирафов и людей одинаковое количество шейных позвонков — по семь.',
    'А вы знали? Банан — это ягода, а клубника — нет.',
    'А вы знали? Первый в мире автомобиль с двигателем внутреннего сгорания разгонялся всего до 16 км/ч.',
    'А вы знали? На Юпитере идут дожди из жидких алмазов.',
    'А вы знали? Сердце синего кита размером с небольшой автомобиль.',
    'А вы знали? Электрический скат может генерировать разряд до 600 вольт.',
    'А вы знали? Гора Эверест каждый год немного подрастает из-за движения тектонических плит.',
    'А вы знали? У улиток около 14 000 зубов, расположенных на языке-тёрке.',
    'А вы знали? Метро в Пхеньяне и Москве — одни из самых глубоких в мире.',
    'А вы знали? Молния нагревает воздух вокруг себя сильнее, чем поверхность Солнца.',
    'А вы знали? Первая в мире автомобильная пробка была зафиксирована ещё в начале XX века в Нью-Йорке.',
    'А вы знали? Дельфины дают своим детёнышам имена — уникальные звуковые сигналы.',
    'А вы знали? Один грамм паутины может быть прочнее стали того же веса.',
    'А вы знали? На Марсе находится самый высокий вулкан Солнечной системы — гора Олимп.',
    'А вы знали? Кровь краба-мечехвоста голубого цвета и используется в медицине для проверки лекарств.',
    'А вы знали? Первый электромобиль был построен раньше, чем машина с бензиновым двигателем.',
    'А вы знали? У бабочек вкусовые рецепторы расположены на лапках.',
    'А вы знали? Сатурн настолько лёгкий, что мог бы плавать в воде, будь такая ванна.',
    'А вы знали? Белые медведи имеют чёрную кожу под белой шерстью.',
    'А вы знали? В среднем автомобиль состоит из более чем 30 000 деталей.',
    'А вы знали? Радуга на самом деле представляет собой полный круг, но с земли обычно видна только её часть.',
    'А вы знали? У осьминога три сердца перестают биться, когда он плывёт — только когда ползёт.',
    'А вы знали? Самая быстрая рыба, парусник, способна разгоняться до 110 км/ч.',
    'А вы знали? Кокосовая пальма может вырасти прямо на пляже из плода, унесённого волнами за сотни километров.',
    'А вы знали? Земля — единственная известная планета, где вода существует в жидком, твёрдом и газообразном состоянии одновременно.',
  ];
  function pickRandomFact() {
    return FACTS[Math.floor(Math.random() * FACTS.length)];
  }

  let splashTargetScreen = 'registerScreen';

  window.addEventListener('load', async () => {
    document.getElementById('splashFact').textContent = pickRandomFact();
    const minWait = new Promise(resolve => setTimeout(resolve, 1800));
    const authCheck = (async () => {
      if (token) {
        const r = await api('/api/me');
        if (r.ok) {
          me = r.data.user; contactsCache = r.data.contacts;
          renderContacts(contactsCache);
          splashTargetScreen = 'dashScreen';
          return;
        } else {
          localStorage.removeItem('chastota_token'); token = null;
        }
      }
      splashTargetScreen = 'registerScreen';
    })();
    await Promise.all([minWait, authCheck]);
    document.getElementById('splashContinueBtn').classList.add('visible');
  });

  document.getElementById('splashContinueBtn').addEventListener('click', () => {
    showScreen(splashTargetScreen);
    if (splashTargetScreen === 'dashScreen') { startPolling(); checkStorageWarning(); }
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
    checkStorageWarning();
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

  document.getElementById('deleteAccountBtn').addEventListener('click', () => {
    const password = prompt('Это удалит твой аккаунт НАВСЕГДА, включая переписки у собеседников (не только у тебя). Введи пароль для подтверждения:');
    if (password === null) return;
    confirmOverlay('Точно удалить аккаунт без возможности восстановить?', async () => {
      const r = await api('/api/delete_account', { method: 'POST', body: { password } });
      if (r.ok) {
        stopPolling();
        localStorage.removeItem('chastota_token');
        token = null; me = null; currentContact = null; sinceId = 0;
        showScreen('registerScreen');
      } else {
        alert(r.data.error || 'Не получилось удалить аккаунт');
      }
    });
  });

  let pendingForward = null; // {text, attachment_type, attachment_data, attachment_duration, attachment_meta, forwarded_from, hideSender}
  let forwardMultiSelected = []; // юзернеймы, в порядке выбора

  function renderContacts(allContacts) {
    checkBirthdays(allContacts);
    updateForwardToolbar();
    const contacts = allContacts.filter(c => c.username !== me.username); // Избранное показываем отдельной кнопкой, не дублируем в списке
    const list = document.getElementById('contactsList');
    list.innerHTML = '';
    if (!contacts.length) {
      list.innerHTML = '<div class="empty-hint">Пока нет переписок. Введи юзернейм выше, чтобы начать.</div>';
      return;
    }
    contacts.forEach(c => {
      const item = document.createElement('div');
      item.className = 'contact-item';
      const pinIcon = c.pinned ? '<span class="pin-icon">📎</span>' : '';
      const unreadBadge = c.unread ? '<span class="unread-badge">' + (c.unread > 99 ? '99+' : c.unread) + '</span>' : '';
      const draft = localStorage.getItem(draftKey(c));
      const previewLine = draft
        ? '<div class="contact-preview draft-label">[Черновик] ' + escapeHtml(draft) + '</div>'
        : '<div class="contact-preview">' + escapeHtml(c.last_preview || '') + '</div>';
      const fwdIndex = forwardMultiSelected.indexOf(c.username);
      const fwdBadge = fwdIndex >= 0 ? '<span class="fwd-select-badge">' + (fwdIndex + 1) + '</span>' : '';
      item.innerHTML = '<div class="avatar-box" style="position:relative;">' + avatarHtml(c) + fwdBadge + '</div><div style="flex:1; min-width:0;"><div class="contact-name">' + escapeHtml(c.name) + officialBadge(c.official) + pinIcon + '</div>' + previewLine + '</div>' + unreadBadge;
      item.addEventListener('click', () => {
        if (pendingForward && forwardMultiSelected.length > 0) {
          toggleForwardSelect(c.username);
        } else {
          openChat(c);
        }
      });
      attachLongPressContact(item, c);
      list.appendChild(item);
    });
  }

  function toggleForwardSelect(username) {
    const idx = forwardMultiSelected.indexOf(username);
    if (idx >= 0) forwardMultiSelected.splice(idx, 1);
    else forwardMultiSelected.push(username);
    renderContacts(contactsCache);
  }

  function updateForwardToolbar() {
    const bar = document.getElementById('forwardToolbar');
    if (!pendingForward) { bar.style.display = 'none'; return; }
    bar.style.display = 'flex';
    const info = document.getElementById('forwardToolbarInfo');
    const captionWrap = document.getElementById('forwardCaptionWrap');
    if (forwardMultiSelected.length > 0) {
      info.textContent = 'Выбрано: ' + forwardMultiSelected.length;
      captionWrap.style.display = 'flex';
    } else {
      info.textContent = 'Нажми на чат, чтобы открыть, или зажми — чтобы выбрать сразу нескольких';
      captionWrap.style.display = 'none';
    }
  }

  function clearPendingForward() {
    pendingForward = null;
    forwardMultiSelected = [];
    document.getElementById('forwardCaptionInput').value = '';
    document.getElementById('forwardPreviewBar').style.display = 'none';
    updateForwardToolbar();
    renderContacts(contactsCache);
  }

  document.getElementById('forwardCancelBtn').addEventListener('click', clearPendingForward);
  document.getElementById('forwardHideSenderBtn').addEventListener('click', () => {
    if (pendingForward) pendingForward.hideSender = !pendingForward.hideSender;
    document.getElementById('forwardHideSenderBtn').style.opacity = pendingForward && pendingForward.hideSender ? '1' : '0.5';
  });
  document.getElementById('forwardSendBtn').addEventListener('click', async () => {
    if (!pendingForward || !forwardMultiSelected.length) return;
    const caption = document.getElementById('forwardCaptionInput').value.trim();
    const targets = forwardMultiSelected.slice();
    const p = pendingForward;
    for (const username of targets) {
      const r = await api('/api/send_message', { method: 'POST', body: {
        to: username, text: caption, attachment_type: p.attachment_type,
        attachment_data: p.attachment_data, attachment_duration: p.attachment_duration,
        attachment_meta: p.attachment_meta, forwarded_from: p.hideSender ? null : p.forwarded_from
      }});
      if (r.ok && currentContact && currentContact.username === username) {
        renderMessage(r.data);
        sinceId = Math.max(sinceId, r.data.id);
      }
    }
    clearPendingForward();
  });

  function attachLongPressContact(item, contact) {
    let timer = null;
    const start = () => { timer = setTimeout(() => {
      if (pendingForward) { toggleForwardSelect(contact.username); }
      else { openChatMenu(contact); }
    }, 500); };
    const cancel = () => { if (timer) clearTimeout(timer); };
    item.addEventListener('touchstart', start);
    item.addEventListener('touchend', cancel);
    item.addEventListener('touchmove', cancel);
    item.addEventListener('mousedown', start);
    item.addEventListener('mouseup', cancel);
    item.addEventListener('mouseleave', cancel);
    item.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      if (pendingForward) { toggleForwardSelect(contact.username); }
      else { openChatMenu(contact); }
    });
  }

  function openChatMenu(contact) {
    closeMessageMenu();
    const overlay = document.createElement('div');
    overlay.className = 'msg-menu-overlay';
    overlay.id = 'msgMenuOverlay';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeMessageMenu(); });
    const menu = document.createElement('div');
    menu.className = 'msg-menu';

    const pinBtn = document.createElement('button');
    pinBtn.textContent = contact.pinned ? 'Открепить' : 'Закрепить';
    pinBtn.addEventListener('click', async () => {
      closeMessageMenu();
      const r = await api('/api/pin_chat', { method: 'POST', body: { contact: contact.username, pin: !contact.pinned } });
      if (r.ok) { contact.pinned = !contact.pinned; refreshContactsFromCache(); }
    });
    menu.appendChild(pinBtn);

    const delBtn = document.createElement('button');
    delBtn.className = 'danger';
    delBtn.textContent = 'Удалить';
    delBtn.addEventListener('click', () => { closeMessageMenu(); openDeleteChatMenu(contact); });
    menu.appendChild(delBtn);

    const blockBtn = document.createElement('button');
    blockBtn.className = contact.blocked_by_me ? '' : 'danger';
    blockBtn.textContent = contact.blocked_by_me ? 'Разблокировать' : 'Заблокировать';
    blockBtn.addEventListener('click', async () => {
      closeMessageMenu();
      const r = await api('/api/block_user', { method: 'POST', body: { contact: contact.username, block: !contact.blocked_by_me } });
      if (r.ok) { contact.blocked_by_me = !contact.blocked_by_me; refreshContactsFromCache(); }
    });
    menu.appendChild(blockBtn);

    const cancelBtn = document.createElement('button');
    cancelBtn.textContent = 'Отмена';
    cancelBtn.addEventListener('click', closeMessageMenu);
    menu.appendChild(cancelBtn);

    overlay.appendChild(menu);
    document.body.appendChild(overlay);
  }

  function openDeleteChatMenu(contact) {
    closeMessageMenu();
    const overlay = document.createElement('div');
    overlay.className = 'msg-menu-overlay';
    overlay.id = 'msgMenuOverlay';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeMessageMenu(); });
    const menu = document.createElement('div');
    menu.className = 'msg-menu';

    const checkRow = document.createElement('label');
    checkRow.className = 'menu-check-row';
    const check = document.createElement('input');
    check.type = 'checkbox';
    checkRow.appendChild(check);
    const checkLabel = document.createElement('span');
    checkLabel.textContent = 'Удалить у всех';
    checkRow.appendChild(checkLabel);
    menu.appendChild(checkRow);

    const confirmBtn = document.createElement('button');
    confirmBtn.className = 'danger';
    confirmBtn.textContent = 'Удалить';
    confirmBtn.addEventListener('click', async () => {
      const everyone = check.checked;
      closeMessageMenu();
      const r = await api('/api/delete_chat', { method: 'POST', body: { contact: contact.username, everyone, secret: !!contact.is_secret } });
      if (contact.is_secret) {
        await api('/api/unset_secret_chat', { method: 'POST', body: { contact: contact.username } });
      }
      if (r.ok) {
        contactsCache = contactsCache.filter(c => c.username !== contact.username);
        renderContacts(contactsCache);
        if (currentContact && currentContact.username === contact.username) {
          saveDraftForCurrent(); currentContact = null; showScreen('dashScreen');
        }
      } else {
        alert('Не получилось удалить чат');
      }
    });
    menu.appendChild(confirmBtn);

    const cancelBtn = document.createElement('button');
    cancelBtn.textContent = 'Отмена';
    cancelBtn.addEventListener('click', closeMessageMenu);
    menu.appendChild(cancelBtn);

    overlay.appendChild(menu);
    document.body.appendChild(overlay);
  }

  function refreshContactsFromCache() {
    if (document.getElementById('dashScreen').classList.contains('active')) renderContacts(contactsCache);
  }

  // --- Поиск ---
  document.getElementById('searchBtn').addEventListener('click', async () => {
    const rawInput = document.getElementById('searchInput').value.trim();
    const username = rawInput.replace('@', '');
    const errEl = document.getElementById('searchError'); errEl.textContent = '';
    const resEl = document.getElementById('searchResult'); resEl.innerHTML = '';
    if (!username) return;
    if (username === me.username) { errEl.textContent = 'Это твой собственный юзернейм'; return; }

    const lower = rawInput.toLowerCase();
    const isFactEasterEgg = (lower === 'интересный факт' || lower === 'факт');

    const panel = document.getElementById('searchRadioPanel');
    const icon = document.getElementById('radioIcon');
    const status = document.getElementById('radioStatus');
    panel.style.display = 'flex';
    icon.classList.remove('not-found');
    document.getElementById('radioWaves').style.display = 'flex';
    status.textContent = 'Поиск...';

    const r = await api('/api/find_user?username=' + encodeURIComponent(username));

    if (r.data.found) {
      panel.style.display = 'none';
      const u = r.data.user;
      const item = document.createElement('div');
      item.className = 'contact-item';
      item.innerHTML = '<div class="avatar-box">' + avatarHtml(u) + '</div><div><div class="contact-name">' + escapeHtml(u.name) + officialBadge(u.official) + '</div><div class="contact-username">@' + escapeHtml(u.username) + '</div></div>';
      item.addEventListener('click', () => { resEl.innerHTML = ''; document.getElementById('searchInput').value = ''; openChat(u); });
      resEl.appendChild(item);
      return;
    }

    document.getElementById('radioWaves').style.display = 'none';
    icon.classList.add('not-found');
    status.textContent = 'Человек не найден';

    if (isFactEasterEgg) {
      const item = document.createElement('div');
      item.className = 'contact-item';
      item.innerHTML = '<div class="avatar-box">🤖</div><div><div class="contact-name">Интересный факт</div><div class="contact-username">бот с фактами</div></div>';
      item.addEventListener('click', () => { resEl.innerHTML = ''; document.getElementById('searchInput').value = ''; openFactBot(); });
      resEl.appendChild(item);
    }
  });

  // --- Бот "Интересный факт" (пасхалка, работает полностью локально, без сервера) ---
  function renderBotMessage(text) {
    const div = document.createElement('div');
    div.className = 'msg';
    div.innerHTML = '<div class="meta">' + formatTime(new Date().toISOString()) + '</div><div class="bubble">' + escapeHtml(text) + '</div>';
    document.getElementById('messages').appendChild(div);
    document.getElementById('messages').scrollTop = document.getElementById('messages').scrollHeight;
  }

  function openFactBot() {
    currentContact = { username: 'factbot', name: 'Интересный факт', avatar: '🤖', avatar_photo: null, online: false, blocked_by_me: false };
    document.getElementById('chatAvatar').innerHTML = avatarHtml(currentContact);
    document.getElementById('chatName').textContent = currentContact.name;
    document.getElementById('chatUsername').textContent = 'бот · отвечает мгновенно';
    document.getElementById('chatTyping').textContent = '';
    document.getElementById('messages').innerHTML = '';
    messagesById = {}; lastRenderedDateKey = '';
    document.getElementById('composer').style.display = 'none';
    document.getElementById('botActionBar').style.display = 'block';
    document.getElementById('botFactBtn').textContent = 'Интересный факт';
    showScreen('chatScreen');
    renderBotMessage('Привет! Жми на кнопку ниже, чтобы получить интересный факт 🤖');
  }

  document.getElementById('botFactBtn').addEventListener('click', () => {
    renderBotMessage(pickRandomFact());
    document.getElementById('botFactBtn').textContent = 'Ещё факт';
  });


  // --- Профиль ---
  document.getElementById('bioBtn').addEventListener('click', () => {
    document.getElementById('bioInput').value = me.bio || '';
    document.getElementById('birthdayInput').value = me.birthday || '';
    document.getElementById('nameInput').value = me.name || '';
    document.getElementById('usernameInput').value = me.username || '';
    document.getElementById('usernameError').textContent = '';
    document.getElementById('privacySelect').value = me.privacy_online || 'all';
    document.getElementById('hideForwardCheck').checked = !!me.hide_forward_link;
    document.getElementById('avatarPreview').innerHTML = avatarHtml(me);
    loadMyPhotos();
    showScreen('bioScreen');
  });
  document.getElementById('bioBackBtn').addEventListener('click', () => showScreen('dashScreen'));
  document.getElementById('bioSaveBtn').addEventListener('click', async () => {
    const bio = document.getElementById('bioInput').value.trim();
    const birthday = document.getElementById('birthdayInput').value || null;
    const name = document.getElementById('nameInput').value.trim();
    const newUsername = document.getElementById('usernameInput').value.trim().replace('@', '');
    const errEl = document.getElementById('usernameError');
    errEl.textContent = '';

    if (newUsername && newUsername !== me.username) {
      const ur = await api('/api/change_username', { method: 'POST', body: { username: newUsername } });
      if (!ur.ok) { errEl.textContent = ur.data.error || 'Не получилось сменить юзернейм'; return; }
      me.username = ur.data.username;
    }
    if (name && name !== me.name) {
      const nr = await api('/api/update_name', { method: 'POST', body: { name } });
      if (nr.ok) me.name = nr.data.name;
    }
    const r = await api('/api/update_bio', { method: 'POST', body: { bio } });
    me.bio = r.data.bio;
    await api('/api/update_birthday', { method: 'POST', 
