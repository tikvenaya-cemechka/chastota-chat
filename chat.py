from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'friends-chat-secret'
socketio = SocketIO(app, cors_allowed_origins="*")

messages = []
online_users = set()

PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Частота — чат для своих</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #12161f; --panel: #1b2130; --panel-raised: #232b3d;
    --accent: #ffb84d; --signal: #5eead4; --text: #e8e6e3;
    --text-dim: #8b93a7; --border: #2a3245;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
  #joinScreen { position: fixed; inset: 0; background: var(--bg); display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 10; gap: 28px; padding: 24px; text-align: center; }
  .logo { font-family: 'Fraunces', serif; font-weight: 700; font-size: 44px; letter-spacing: -0.02em; color: var(--text); }
  .logo .dot { color: var(--accent); }
  .tagline { font-family: 'IBM Plex Mono', monospace; color: var(--text-dim); font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase; margin-top: -18px; }
  #joinScreen input { background: var(--panel); border: 1px solid var(--border); color: var(--text); font-family: 'IBM Plex Mono', monospace; font-size: 16px; padding: 14px 18px; border-radius: 10px; width: 260px; outline: none; transition: border-color 0.2s; }
  #joinScreen input:focus { border-color: var(--accent); }
  #joinScreen button { background: var(--accent); color: #1b1204; font-family: 'Inter', sans-serif; font-weight: 600; font-size: 15px; padding: 14px 32px; border: none; border-radius: 10px; cursor: pointer; transition: transform 0.15s, opacity 0.15s; }
  #joinScreen button:hover { transform: translateY(-1px); opacity: 0.92; }
  #app { display: none; flex-direction: column; height: 100%; }
  header { display: flex; align-items: center; justify-content: space-between; padding: 14px 20px; border-bottom: 1px solid var(--border); background: var(--panel); }
  header .brand { font-family: 'Fraunces', serif; font-weight: 700; font-size: 20px; }
  header .brand .dot { color: var(--accent); }
  #userList { font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--text-dim); display: flex; align-items: center; gap: 8px; }
  #userList .pulse { width: 8px; height: 8px; border-radius: 50%; background: var(--signal); animation: pulse 2s infinite; }
  @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(94,234,212,0.5); } 70% { box-shadow: 0 0 0 8px rgba(94,234,212,0); } 100% { box-shadow: 0 0 0 0 rgba(94,234,212,0); } }
  #messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 4px; }
  .msg { max-width: 78%; animation: rise 0.18s ease-out; padding: 2px 0; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
  .msg .meta { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--text-dim); margin-bottom: 3px; display: flex; gap: 8px; }
  .msg .meta .name { color: var(--signal); font-weight: 600; }
  .msg .bubble { background: var(--panel-raised); border-radius: 4px 12px 12px 12px; padding: 10px 14px; font-size: 14.5px; line-height: 1.45; word-wrap: break-word; }
  .msg.own { align-self: flex-end; }
  .msg.own .meta { justify-content: flex-end; }
  .msg.own .bubble { background: var(--accent); color: #1b1204; border-radius: 12px 4px 12px 12px; }
  .msg.own .meta .name { color: #d99a3a; }
  .system-msg { align-self: center; font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; color: var(--text-dim); padding: 4px 12px; border: 1px dashed var(--border); border-radius: 20px; margin: 6px 0; }
  #composer { display: flex; gap: 10px; padding: 16px 20px; border-top: 1px solid var(--border); background: var(--panel); }
  #textInput { flex: 1; background: var(--panel-raised); border: 1px solid var(--border); color: var(--text); font-family: 'Inter', sans-serif; font-size: 14.5px; padding: 12px 16px; border-radius: 10px; outline: none; }
  #textInput:focus { border-color: var(--accent); }
  #sendBtn { background: var(--accent); color: #1b1204; border: none; border-radius: 10px; padding: 0 22px; font-weight: 600; font-family: 'Inter', sans-serif; cursor: pointer; transition: opacity 0.15s; }
  #sendBtn:hover { opacity: 0.9; }
  #messages::-webkit-scrollbar { width: 6px; }
  #messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>
<div id="joinScreen">
  <div>
    <div class="logo">Частота<span class="dot">.</span></div>
    <div class="tagline">свой канал связи</div>
  </div>
  <input type="text" id="usernameInput" placeholder="Как тебя зовут?" maxlength="20" autocomplete="off">
  <button id="joinBtn">Выйти на связь</button>
</div>
<div id="app">
  <header>
    <div class="brand">Частота<span class="dot">.</span></div>
    <div id="userList"><span class="pulse"></span><span id="userListText">на связи: —</span></div>
  </header>
  <div id="messages"></div>
  <div id="composer">
    <input type="text" id="textInput" placeholder="Сообщение..." autocomplete="off">
    <button id="sendBtn">Отправить</button>
  </div>
</div>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
  const socket = io();
  let username = '';
  const joinScreen = document.getElementById('joinScreen');
  const appEl = document.getElementById('app');
  const usernameInput = document.getElementById('usernameInput');
  const joinBtn = document.getElementById('joinBtn');
  const messagesEl = document.getElementById('messages');
  const textInput = document.getElementById('textInput');
  const sendBtn = document.getElementById('sendBtn');
  const userListText = document.getElementById('userListText');

  function join() {
    const name = usernameInput.value.trim();
    if (!name) return;
    username = name;
    joinScreen.style.display = 'none';
    appEl.style.display = 'flex';
    socket.emit('join', { username });
    textInput.focus();
  }
  joinBtn.addEventListener('click', join);
  usernameInput.addEventListener('keydown', e => { if (e.key === 'Enter') join(); });

  function renderMessage(msg) {
    const div = document.createElement('div');
    const isOwn = msg.username === username;
    div.className = 'msg' + (isOwn ? ' own' : '');
    div.innerHTML = '<div class="meta"><span class="name">' + escapeHtml(msg.username) + '</span><span>' + msg.time + '</span></div><div class="bubble">' + escapeHtml(msg.text) + '</div>';
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
  function renderSystem(text) {
    const div = document.createElement('div');
    div.className = 'system-msg';
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
  function escapeHtml(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }
  socket.on('history', (msgs) => { messagesEl.innerHTML = ''; msgs.forEach(renderMessage); });
  socket.on('new_message', renderMessage);
  socket.on('system_message', renderSystem);
  socket.on('user_list', (users) => { userListText.textContent = users.length ? ('на связи: ' + users.join(', ')) : 'на связи: —'; });

  function send() {
    const text = textInput.value.trim();
    if (!text) return;
    socket.emit('send_message', { username, text });
    textInput.value = '';
  }
  sendBtn.addEventListener('click', send);
  textInput.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
  window.addEventListener('beforeunload', () => { socket.emit('leave', { username }); });
</script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(PAGE)


@socketio.on('join')
def handle_join(data):
    username = data.get('username', 'Гость')
    online_users.add(username)
    emit('history', messages)
    emit('user_list', list(online_users), broadcast=True)
    emit('system_message', f'{username} присоединился к чату', broadcast=True)


@socketio.on('leave')
def handle_leave(data):
    username = data.get('username', 'Гость')
    online_users.discard(username)
    emit('user_list', list(online_users), broadcast=True)
    emit('system_message', f'{username} вышел из чата', broadcast=True)


@socketio.on('send_message')
def handle_message(data):
    msg = {
        'username': data.get('username', 'Гость'),
        'text': data.get('text', ''),
        'time': datetime.now().strftime('%H:%M')
    }
    messages.append(msg)
    if len(messages) > 200:
        messages.pop(0)
    emit('new_message', msg, broadcast=True)


if __name__ == '__main__':
    print("Чат запущен! Открой в браузере: http://localhost:5000")
    print("Друзья в той же сети (wifi) могут зайти по твоему локальному IP, например http://192.168.1.5:5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
