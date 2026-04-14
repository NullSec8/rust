import os
import socket
import threading
import requests
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, make_response, jsonify
from flask_socketio import SocketIO, send, emit, disconnect

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key')
PASSWORD = os.environ.get('SITE_PASSWORD', 'scuby123')

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', manage_session=True)

PORT = 12345
cli_clients = []  # CLI clients
chat_history = []  # Store chat history
online_users = 0  # Track connected web users

# --- Helper to get geolocation from IP (fallback if no precise coords) ---
def get_geolocation(ip):
    try:
        response = requests.get(f"http://ip-api.com/json/{ip}", timeout=5)
        data = response.json()
        if data.get('status') == 'success':
            return {
                'country': data.get('country'),
                'region': data.get('regionName'),
                'city': data.get('city'),
                'lat': data.get('lat'),
                'lon': data.get('lon'),
                'source': 'ip'
            }
    except Exception as e:
        print(f"[GEO] Failed to get location: {e}")
    return None

# --- Helper to log user data (JSON lines) ---
def log_user_data(username, ip, location, precise_location=None):
    """
    location: approx location dict (or None)
    precise_location: dict like {'lat': ..., 'lon': ..., 'accuracy': ...} if provided by client
    """
    data = {
        "timestamp": datetime.utcnow().isoformat(),
        "username": username,
        "ip": ip,
        "location": location,  # approx/ip-based (can be None)
        "precise_location": precise_location  # exact coords from client (can be None)
    }
    # append as one JSON-per-line for easy parsing later
    with open("user_data.txt", "a", encoding="utf-8") as f:
        f.write(json.dumps(data) + "\n")

# --- Routes ---
@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    username = request.cookies.get('username', 'Guest')

    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    approx_location = get_geolocation(user_ip)  # fallback
    print(f"[INFO] User '{username}' IP: {user_ip}, ApproxLocation: {approx_location}")

    # log connection using approx location for now (precise may be sent by client later)
    log_user_data(username, user_ip, approx_location, precise_location=None)

    return render_template('index.html', username=username)


@app.route('/paint')
@app.route('/paint.html')
def paint():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    username = request.cookies.get('username', 'Guest')
    return render_template('paint.html', username=username)

@app.route('/music')
def music():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    username = request.cookies.get('username', 'Guest')
    return render_template('music.html', username=username)

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        pw = request.form.get('password', '')
        if pw == PASSWORD:
            session['logged_in'] = True
            resp = make_response(redirect(url_for('index')))
            resp.set_cookie('username', 'GuestUser', max_age=30*24*60*60)
            return resp
        else:
            error = "Incorrect password."
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    resp = make_response(redirect(url_for('login')))
    resp.set_cookie('username', '', expires=0)
    return resp

@app.route('/set_username', methods=['POST'])
def set_username():
    new_name = request.form.get('username', 'Guest')
    resp = make_response(redirect(url_for('index')))
    resp.set_cookie('username', new_name, max_age=30*24*60*60)
    return resp

# --- New endpoint: client reports precise coords (called from browser with user's consent) ---
@app.route('/report_location', methods=['POST'])
def report_location():
    """
    Expects JSON body: { "lat": float, "lon": float, "accuracy": float (optional) }
    The request should come from a logged-in user (session).
    """
    if not session.get('logged_in'):
        return jsonify({"error": "not authenticated"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid json"}), 400

    try:
        lat = float(data.get('lat'))
        lon = float(data.get('lon'))
        accuracy = float(data.get('accuracy', 0.0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid lat/lon"}), 400

    username = request.cookies.get('username', 'Guest')
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    approx_location = get_geolocation(user_ip)

    precise = {"lat": lat, "lon": lon, "accuracy": accuracy, "source": "browser_geolocation"}
    # Log both approx and precise
    log_user_data(username, user_ip, approx_location, precise_location=precise)

    print(f"[GEO-RECV] {username} reported precise location: {precise} (IP: {user_ip})")
    return jsonify({"status": "ok"}), 200

# --- SocketIO handlers ---
@socketio.on('connect')
def handle_connect():
    if not session.get('logged_in'):
        disconnect()
        return

    global online_users
    online_users += 1

    username = request.cookies.get('username', 'Guest')
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    approx_location = get_geolocation(user_ip)

    # log connection (approx). Client may call /report_location later to supply precise coords.
    log_user_data(username, user_ip, approx_location, precise_location=None)

    print(f"[WEB] {username} connected — IP: {user_ip}, ApproxLocation: {approx_location} (Total: {online_users})")

    for msg in chat_history:
        emit('message', msg)

    emit('userCount', online_users, broadcast=True)

@socketio.on('message')
def handle_web_message(msg):
    print(f"[WEB] Received: {msg}")
    chat_history.append(msg)
    send(msg, broadcast=True)

    # Managing dead CLI clients
    dead_clients = []
    for client in cli_clients:
        try:
            client.sendall(msg.encode('utf-8'))
        except Exception:
            dead_clients.append(client)
    for dc in dead_clients:
        try:
            cli_clients.remove(dc)
            dc.close()
        except Exception:
            pass

@socketio.on('typing')
def handle_typing(username):
    emit('typing', username, broadcast=True, include_self=False)

@socketio.on('disconnect')
def handle_disconnect():
    global online_users
    try:
        online_users -= 1
        if online_users < 0:
            online_users = 0
    except Exception:
        pass
    print(f"[WEB] User disconnected (Total: {online_users})")
    emit('userCount', online_users, broadcast=True)

# --- CLI socket server (unchanged) ---
def handle_cli_client(conn, addr):
    cli_clients.append(conn)
    try:
        while True:
            msg = conn.recv(4096)
            if not msg:
                break
            msg = msg.decode('utf-8')
            chat_history.append(msg)
            socketio.emit('message', msg)

            dead_clients = []
            for client in cli_clients:
                if client != conn:
                    try:
                        client.sendall(msg.encode('utf-8'))
                    except Exception:
                        dead_clients.append(client)
            for dc in dead_clients:
                try:
                    cli_clients.remove(dc)
                    dc.close()
                except Exception:
                    pass
    finally:
        if conn in cli_clients:
            cli_clients.remove(conn)
        conn.close()

def start_cli_server():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(('0.0.0.0', PORT))
    server_socket.listen()
    while True:
        try:
            conn, addr = server_socket.accept()
            threading.Thread(target=handle_cli_client, args=(conn, addr), daemon=True).start()
        except Exception as e:
            print(f"[CLI] Error accepting connection: {e}")


# --- Collaborative Paint Events ---
@socketio.on('draw', namespace='/paint')
def handle_draw_event(data):
    """
    Broadcast small drawing events like:
    { tool, color, size, from: [x,y], to: [x2,y2] }
    """
    emit('draw', data, broadcast=True, include_self=False, namespace='/paint')

@socketio.on('clear', namespace='/paint')
def handle_clear_event():
    emit('clear', broadcast=True, include_self=False, namespace='/paint')

def cloudflared_tunnel():
    import subprocess
    try:
        process = subprocess.Popen(['cloudflared', 'tunnel', '--url', f'http://localhost:{PORT}'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for line in process.stdout:
            print(f"[TUNNEL] {line.decode().strip()}")
    except Exception as e:
        print(f"[TUNNEL] Failed to start Cloudflared tunnel: {e}")

if __name__ == '__main__':
    threading.Thread(target=start_cli_server, daemon=True).start()
    threading.Thread(target=cloudflared_tunnel, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5000)
