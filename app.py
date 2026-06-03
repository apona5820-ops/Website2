import os, sqlite3, zipfile, subprocess, signal, shutil, psutil, time, datetime, re
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit
from datetime import datetime, timedelta
import pytz

# Global process tracker
running_procs = {}
start_times = {}

# Initialize SocketIO
socketio = SocketIO()

def get_db():
    db_path = os.path.join(os.getcwd(), 'storage/nehost.db')
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs('storage', exist_ok=True)
    db = get_db()
    # Users table with extra fields for registration
    db.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        fname TEXT, lname TEXT, username TEXT, email TEXT, password TEXT, pfp TEXT DEFAULT 'default.png',
        role TEXT DEFAULT 'free', 
        status TEXT DEFAULT 'active',
        server_limit INTEGER DEFAULT 10,
        notifications TEXT DEFAULT '',
        disk INTEGER DEFAULT 500,
        memory_limit TEXT DEFAULT '512MB',
        created_at TEXT,
        expiry_date TEXT
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER, name TEXT, folder TEXT, 
        status TEXT, startup TEXT, pid INTEGER,
        server_status TEXT DEFAULT 'active'
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, subject TEXT, message TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS admin_settings (
        id INTEGER PRIMARY KEY, 
        username TEXT, password TEXT,
        popup_title TEXT, popup_msg TEXT, popup_img TEXT, show_popup INTEGER DEFAULT 0
    )''')
    if not db.execute('SELECT * FROM admin_settings WHERE id=1').fetchone():
        db.execute('INSERT INTO admin_settings (id, username, password) VALUES (1, "apon20", "10000")')
    
    # Add missing columns to existing users table (if upgrading)
    try:
        db.execute('ALTER TABLE users ADD COLUMN disk INTEGER DEFAULT 500')
    except: pass
    try:
        db.execute('ALTER TABLE users ADD COLUMN memory_limit TEXT DEFAULT "512MB"')
    except: pass
    try:
        db.execute('ALTER TABLE users ADD COLUMN created_at TEXT')
    except: pass
    try:
        db.execute('ALTER TABLE users ADD COLUMN expiry_date TEXT')
    except: pass
    
    db.commit()
    db.close()

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'nehost_ultra_pro_max_99'
    app.config['BASE_STORAGE'] = os.path.join(os.getcwd(), 'storage/instances')
    app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'static/uploads')
    os.makedirs(app.config['BASE_STORAGE'], exist_ok=True)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    init_db()
    socketio.init_app(app)

    def get_precise_uptime(ts):
        if not ts: return "Offline"
        diff = int(time.time() - ts)
        m, rem = divmod(diff, 2592000)
        d, rem = divmod(rem, 86400)
        h, rem = divmod(rem, 3600)
        mn, _ = divmod(rem, 60)
        parts = []
        if m: parts.append(f"{m}mo")
        if d: parts.append(f"{d}d")
        if h: parts.append(f"{h}h")
        parts.append(f"{mn}m")
        return " ".join(parts)

    # ---------- LANDING PAGE ----------
    @app.route('/')
    def home():
        if 'user_id' in session:
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))

    # ---------- REGISTER (ONLY API, NO HTML) ----------
    @app.route('/register', methods=['GET', 'POST'])
    def register_user():
        if request.method == 'GET':
            u = request.args.get('u')
            p = request.args.get('p')
            disk = request.args.get('disk')
            memory_input = request.args.get('memory', '512MB')
            days_input = request.args.get('days', '30d')
        else:
            u = request.form.get('u')
            p = request.form.get('p')
            disk = request.form.get('disk')
            memory_input = request.form.get('memory', '512MB')
            days_input = request.form.get('days', '30d')

        if not u or not p:
            return jsonify({"status": "error", "msg": "Username (u) and Password (p) are required!"})

        # Kolkata timezone
        kolkata_tz = pytz.timezone('Asia/Kolkata')
        now = datetime.now(kolkata_tz)
        
        # Expiry parsing
        match_days = re.match(r"(\d+)([a-zA-Z]+)", days_input)
        if match_days:
            value = int(match_days.group(1))
            unit = match_days.group(2).lower()
            if unit in ['d', 'day', 'days']: expiry_time = now + timedelta(days=value)
            elif unit in ['h', 'hour', 'hours']: expiry_time = now + timedelta(hours=value)
            elif unit in ['m', 'min', 'minute', 'minutes']: expiry_time = now + timedelta(minutes=value)
            elif unit in ['month', 'months']: expiry_time = now + timedelta(days=value * 30)
            elif unit in ['y', 'year', 'years']: expiry_time = now + timedelta(days=value * 365)
            else: expiry_time = now + timedelta(days=value)
        else:
            expiry_time = now + timedelta(days=30)

        expiry_str = expiry_time.strftime('%d-%m-%Y %H:%M:%S')
        created_str = now.strftime('%d-%m-%Y %H:%M:%S')

        # Memory unit support
        final_memory = memory_input.upper()
        if not any(unit in final_memory for unit in ['KB', 'MB', 'GB']):
            final_memory += 'MB'

        disk_val = int(disk) if disk else 500
        
        db = get_db()
        existing = db.execute('SELECT id FROM users WHERE username=?', (u,)).fetchone()
        if existing:
            db.close()
            return jsonify({"status": "error", "msg": "Username already taken!"})
        
        db.execute('''INSERT INTO users 
            (username, password, disk, memory_limit, created_at, expiry_date, server_limit, role, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (u, p, disk_val, final_memory, created_str, expiry_str, 10, 'free', 'active'))
        db.commit()
        db.close()

        # Create user folder and default main.py
        user_folder = os.path.join(app.config['BASE_STORAGE'], u)
        os.makedirs(user_folder, exist_ok=True)
        with open(os.path.join(user_folder, 'main.py'), 'w') as f:
            f.write(f"# ARAFAT Hosting\n# User: {u}\n# Expiry: {expiry_str}\n\nprint('Hello {u}!')")

        return jsonify({
            "status": "success",
            "msg": f"User '{u}' created! Validity: {days_input} 🚀",
            "details": {
                "username": u,
                "memory_limit": final_memory,
                "expiry": expiry_str,
                "timezone": "Kolkata (IST)"
            }
        })

    # ---------- LOGIN (with expiry check) ----------
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE username=? AND password=?', (username, password)).fetchone()
            if not user:
                return jsonify({'status': 'error', 'msg': 'Invalid credentials! Please register first.'}), 401
            # Check expiry
            if user['expiry_date']:
                try:
                    kolkata_tz = pytz.timezone('Asia/Kolkata')
                    now = datetime.now(kolkata_tz)
                    exp = datetime.strptime(user['expiry_date'], '%d-%m-%Y %H:%M:%S')
                    exp = kolkata_tz.localize(exp)
                    if now > exp:
                        return jsonify({'status': 'error', 'msg': 'Account expired. Contact admin.'}), 403
                except:
                    pass
            if user['status'] != 'active':
                return jsonify({'status': 'error', 'msg': 'Account suspended.'}), 403
            session['user_id'] = user['id']
            return jsonify({'status': 'success', 'url': url_for('dashboard')}), 200
        return render_template('web/login.html')

    @app.route('/dashboard')
    def dashboard():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        db.close()
        if not user or user['status'] != 'active':
            session.clear()
            return redirect(url_for('login'))
        return render_template('web/dashboard.html', user=user)

    # ---------- SERVER MANAGEMENT ----------
    @app.route('/add', methods=['POST'])
    def add_srv():
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        count = db.execute('SELECT COUNT(*) as count FROM servers WHERE user_id=?', (session['user_id'],)).fetchone()['count']
        if count >= user['server_limit']:
            db.close()
            return jsonify({'status': 'error', 'msg': f'Limit reached! Max {user["server_limit"]}'})
        name = request.json.get('name')
        if not name:
            return jsonify({'status': 'error', 'msg': 'Server name required'})
        folder = secure_filename(name).lower() + "_" + str(int(time.time()))
        try:
            db.execute('INSERT INTO servers (user_id, name, folder, status, startup) VALUES (?,?,?,?,?)',
                       (session['user_id'], name, folder, 'Offline', 'main.py'))
            db.commit()
            server_path = os.path.join(app.config['BASE_STORAGE'], folder)
            os.makedirs(server_path, exist_ok=True)
            db.close()
            return jsonify({'status': 'success'})
        except Exception as e:
            db.close()
            return jsonify({'status': 'error', 'msg': str(e)})

    @app.route('/servers')
    def list_servers():
        if 'user_id' not in session:
            return jsonify({'servers': []})
        db = get_db()
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (session['user_id'],)).fetchall()
        db.close()
        srvs = []
        for r in rows:
            f, saved_pid = r['folder'], r['pid']
            online = False
            if saved_pid and psutil.pid_exists(saved_pid):
                try:
                    p = psutil.Process(saved_pid)
                    if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                        online = True
                except:
                    pass
            elif f in running_procs and running_procs[f].poll() is None:
                online = True
            cpu, ram = "0%", "0MB"
            if online:
                try:
                    pid_to_use = running_procs[f].pid if f in running_procs else saved_pid
                    proc = psutil.Process(pid_to_use)
                    cpu = f"{proc.cpu_percent(interval=None)}%"
                    ram = f"{proc.memory_info().rss / (1024*1024):.1f}MB"
                except:
                    pass
            srvs.append({
                'name': r['name'],
                'folder': f,
                'online': online,
                'startup': r['startup'],
                'uptime': get_precise_uptime(start_times.get(f)),
                'cpu': cpu,
                'ram': ram,
                'status': r['server_status']
            })
        return jsonify({'servers': srvs})

    @app.route('/server/action/<folder>/<act>', methods=['POST'])
    def server_action(folder, act):
        db = get_db()
        srv = db.execute('SELECT server_status FROM servers WHERE folder=?', (folder,)).fetchone()
        if srv and srv['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Server suspended by Admin'})
        path = os.path.join(app.config['BASE_STORAGE'], folder)
        log_path = os.path.join(path, 'console.log')
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if act == 'install':
            req = os.path.join(path, 'requirements.txt')
            if os.path.exists(req):
                with open(log_path, 'a') as f:
                    f.write(f"\n[{now}] 📦 Installing requirements...\n")
                subprocess.Popen(['pip', 'install', '-r', 'requirements.txt'], cwd=path, stdout=open(log_path, 'a'), stderr=open(log_path, 'a'))
                return jsonify({'status': 'installing'})
            return jsonify({'status': 'error', 'msg': 'requirements.txt missing'})
        if act in ['start', 'restart']:
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            old_pid = row['pid'] if row else None
            if folder in running_procs or (old_pid and psutil.pid_exists(old_pid)):
                try:
                    kill_pid = running_procs[folder].pid if folder in running_procs else old_pid
                    os.killpg(os.getpgid(kill_pid), signal.SIGKILL)
                except:
                    pass
            srv_startup = db.execute('SELECT startup FROM servers WHERE folder=?', (folder,)).fetchone()
            startup_file = srv_startup['startup'] if srv_startup else 'main.py'
            with open(log_path, 'a') as f:
                f.write(f"\n[{now}] 🚀 Starting {startup_file}\n")
            proc = subprocess.Popen(['python3', startup_file], cwd=path, stdout=open(log_path, 'a'), stderr=open(log_path, 'a'), preexec_fn=os.setsid)
            running_procs[folder] = proc
            start_times[folder] = time.time()
            db.execute('UPDATE servers SET pid=? WHERE folder=?', (proc.pid, folder))
            db.commit()
            db.close()
            return jsonify({'status': 'started'})
        elif act == 'stop':
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            t_pid = running_procs[folder].pid if folder in running_procs else (row['pid'] if row else None)
            if t_pid:
                try:
                    os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except:
                    pass
            if folder in running_procs:
                del running_procs[folder]
            db.execute('UPDATE servers SET pid=NULL WHERE folder=?', (folder,))
            db.commit()
            db.close()
            with open(log_path, 'a') as f:
                f.write(f"\n[{now}] 🛑 Stopped\n")
            return jsonify({'status': 'stopped'})
        return jsonify({'status': 'ok'})

    @app.route('/server/log/<folder>')
    def server_log(folder):
        path = os.path.join(app.config['BASE_STORAGE'], folder, 'console.log')
        online = folder in running_procs and running_procs[folder].poll() is None
        uptime = get_precise_uptime(start_times.get(folder)) if online else 'Offline'
        log_content = ''
        if os.path.exists(path):
            with open(path, 'r') as f:
                log_content = f.read()[-5000:]
        return jsonify({'log': log_content, 'online': online, 'uptime': uptime})

    @app.route('/server/command/<folder>', methods=['POST'])
    def send_command(folder):
        cmd = request.json.get('command')
        if not cmd:
            return jsonify({'status': 'error'})
        path = os.path.join(app.config['BASE_STORAGE'], folder)
        try:
            proc = subprocess.Popen(cmd, shell=True, cwd=path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = proc.communicate(timeout=5)
            output = stdout + stderr
            log_path = os.path.join(path, 'console.log')
            with open(log_path, 'a') as f:
                f.write(f"\n[CMD] $ {cmd}\n{output}\n")
            return jsonify({'output': output})
        except Exception as e:
            return jsonify({'output': str(e)})

    @app.route('/server/delete/<folder>', methods=['POST'])
    def delete_server(folder):
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        db = get_db()
        srv = db.execute('SELECT server_status, pid FROM servers WHERE folder=?', (folder,)).fetchone()
        if srv and srv['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Suspended server cannot be deleted'})
        t_pid = running_procs[folder].pid if folder in running_procs else (srv['pid'] if srv else None)
        if t_pid:
            try:
                os.killpg(os.getpgid(t_pid), signal.SIGKILL)
            except:
                pass
        if folder in running_procs:
            del running_procs[folder]
        db.execute('DELETE FROM servers WHERE folder=?', (folder,))
        db.commit()
        db.close()
        shutil.rmtree(os.path.join(app.config['BASE_STORAGE'], folder), ignore_errors=True)
        return jsonify({'status': 'deleted'})

    # ---------- FILE MANAGER ROUTES ----------
    @app.route('/files/list/<folder>')
    def flist(folder):
        sub = request.args.get('path', '')
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub)
        if not os.path.exists(base):
            return jsonify([])
        items = []
        for f in sorted(os.listdir(base)):
            if f == 'console.log':
                continue
            p = os.path.join(base, f)
            stat = os.stat(p) if os.path.exists(p) else None
            items.append({
                'name': f,
                'is_dir': os.path.isdir(p),
                'is_zip': f.lower().endswith('.zip'),
                'size': stat.st_size if stat else 0,
                'modified': stat.st_mtime if stat else None
            })
        return jsonify(items)

    @app.route('/files/content/<folder>/<name>')
    def fcontent(folder, name):
        sub = request.args.get('path', '')
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub, name)
        try:
            with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                return jsonify({'content': f.read()})
        except:
            return jsonify({'content': ''})

    @app.route('/files/save/<folder>/<name>', methods=['POST'])
    def fsave(folder, name):
        sub = request.args.get('path', '')
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub, name)
        try:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(request.json.get('content', ''))
            return jsonify({'status': 'saved'})
        except:
            return jsonify({'status': 'error'})

    @app.route('/files/delete-bulk/<folder>', methods=['POST'])
    def delete_bulk(folder):
        data = request.json
        sub = data.get('path', '')
        names = data.get('names', [])
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub)
        for name in names:
            p = os.path.join(base, name)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        return jsonify({'status': 'ok'})

    @app.route('/files/create-file/<folder>', methods=['POST'])
    def create_file(folder):
        data = request.json
        sub = data.get('path', '')
        name = secure_filename(data.get('name', ''))
        if not name:
            return jsonify({'status': 'error'})
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub, name)
        with open(p, 'w') as f:
            f.write('')
        return jsonify({'status': 'success'})

    @app.route('/files/create-folder/<folder>', methods=['POST'])
    def create_folder(folder):
        data = request.json
        sub = data.get('path', '')
        name = secure_filename(data.get('name', ''))
        if not name:
            return jsonify({'status': 'error'})
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub, name)
        os.makedirs(p, exist_ok=True)
        return jsonify({'status': 'success'})

    @app.route('/files/upload/<folder>', methods=['POST'])
    def upload_file(folder):
        sub = request.form.get('path', '')
        file = request.files['file']
        dest = os.path.join(app.config['BASE_STORAGE'], folder, sub)
        os.makedirs(dest, exist_ok=True)
        file.save(os.path.join(dest, secure_filename(file.filename)))
        return jsonify({'status': 'success'})

    @app.route('/files/rename/<folder>', methods=['POST'])
    def rename_file(folder):
        data = request.json
        sub = data.get('path', '')
        old = data.get('old')
        new = data.get('new')
        if not old or not new:
            return jsonify({'status': 'error'})
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub)
        os.rename(os.path.join(base, old), os.path.join(base, new))
        return jsonify({'status': 'success'})

    @app.route('/files/download/<folder>/<name>')
    def download_file(folder, name):
        sub = request.args.get('path', '')
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub, name)
        return send_file(p, as_attachment=True)

    @app.route('/files/unzip/<folder>', methods=['POST'])
    def unzip_file(folder):
        data = request.json
        zip_name = data.get('name')
        sub = data.get('path', '')
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub)
        zip_path = os.path.join(base, zip_name)
        if os.path.exists(zip_path) and zipfile.is_zipfile(zip_path):
            with zipfile.ZipFile(zip_path, 'r') as z:
                z.extractall(base)
            return jsonify({'status': 'success'})
        return jsonify({'status': 'error', 'msg': 'Invalid zip'})

    # ---------- ADMIN ROUTES ----------
    @app.route('/admin-login', methods=['GET', 'POST'])
    def admin_login():
        if request.method == 'POST':
            u = request.form.get('username')
            p = request.form.get('password')
            db = get_db()
            admin = db.execute('SELECT * FROM admin_settings WHERE username=? AND password=?', (u, p)).fetchone()
            db.close()
            if admin:
                session['admin_logged'] = True
                return redirect(url_for('admin_panel'))
        return render_template('web/admin_login.html')

    @app.route('/admin/panel')
    def admin_panel():
        if not session.get('admin_logged'):
            return redirect(url_for('admin_login'))
        return render_template('web/admin_panel.html')

    @app.route('/admin/stats')
    def admin_stats():
        if not session.get('admin_logged'):
            return jsonify({})
        db = get_db()
        users = db.execute('SELECT * FROM users').fetchall()
        user_list = []
        for u in users:
            srvs = db.execute('SELECT * FROM servers WHERE user_id=?', (u['id'],)).fetchall()
            active_srvs = 0
            for s in srvs:
                if s['pid'] and psutil.pid_exists(s['pid']):
                    try:
                        p = psutil.Process(s['pid'])
                        if p.is_running():
                            active_srvs += 1
                    except:
                        pass
                elif s['folder'] in running_procs and running_procs[s['folder']].poll() is None:
                    active_srvs += 1
            user_list.append({
                'id': u['id'],
                'fname': u['fname'] or u['username'],
                'email': u['email'] or f"{u['username']}@drift.local",
                'srv_count': len(srvs),
                'active_srvs': active_srvs,
                'status': u['status'],
                'role': u['role'],
                'server_limit': u['server_limit']
            })
        db.close()
        return jsonify({'users': user_list, 'sys_cpu': f"{psutil.cpu_percent()}%", 'sys_ram': f"{psutil.virtual_memory().percent}%"})

    @app.route('/admin/user/update', methods=['POST'])
    def update_user():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error'})
        data = request.json
        db = get_db()
        db.execute('UPDATE users SET role=?, status=?, server_limit=? WHERE id=?',
                   (data['role'], data['status'], data['limit'], data['user_id']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/create-user', methods=['POST'])
    def admin_create_user():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error'})
        data = request.json
        db = get_db()
        db.execute('INSERT INTO users (fname, email, password, server_limit) VALUES (?,?,?,?)',
                   (data['name'], data['email'], data['pass'], data.get('limit', 1)))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/delete-user/<int:uid>', methods=['POST'])
    def delete_user(uid):
        if not session.get('admin_logged'):
            return jsonify({'status': 'error'})
        db = get_db()
        srvs = db.execute('SELECT folder FROM servers WHERE user_id=?', (uid,)).fetchall()
        for s in srvs:
            shutil.rmtree(os.path.join(app.config['BASE_STORAGE'], s['folder']), ignore_errors=True)
        db.execute('DELETE FROM servers WHERE user_id=?', (uid,))
        db.execute('DELETE FROM users WHERE id=?', (uid,))
        db.commit()
        db.close()
        return jsonify({'status': 'deleted'})

    @app.route('/admin/login-as/<int:uid>')
    def login_as(uid):
        if not session.get('admin_logged'):
            return redirect(url_for('admin_login'))
        session['user_id'] = uid
        return redirect(url_for('dashboard'))

    @app.route('/admin/manage-user/<int:uid>')
    def admin_manage_user(uid):
        if not session.get('admin_logged'):
            return redirect(url_for('admin_login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        servers = db.execute('SELECT * FROM servers WHERE user_id=?', (uid,)).fetchall()
        db.close()
        return render_template('web/admin_manage_user.html', user=user, servers=servers)

    @app.route('/admin/suspend-server/<int:sid>', methods=['POST'])
    def admin_suspend_server(sid):
        if not session.get('admin_logged'):
            return jsonify({'status': 'error'})
        status = request.json.get('status')
        db = get_db()
        db.execute('UPDATE servers SET server_status=? WHERE id=?', (status, sid))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/delete-server/<int:sid>', methods=['POST'])
    def admin_delete_server(sid):
        if not session.get('admin_logged'):
            return jsonify({'status': 'error'})
        db = get_db()
        srv = db.execute('SELECT folder FROM servers WHERE id=?', (sid,)).fetchone()
        if srv:
            folder = srv['folder']
            if folder in running_procs:
                try:
                    os.killpg(os.getpgid(running_procs[folder].pid), signal.SIGKILL)
                except:
                    pass
                del running_procs[folder]
            db.execute('DELETE FROM servers WHERE id=?', (sid,))
            db.commit()
            shutil.rmtree(os.path.join(app.config['BASE_STORAGE'], folder), ignore_errors=True)
        db.close()
        return jsonify({'status': 'deleted'})

    @app.route('/admin/files/<folder>')
    def admin_browse_files(folder):
        if not session.get('admin_logged'):
            return redirect(url_for('admin_login'))
        return render_template('web/dashboard.html', user={'fname': 'Admin'}, is_admin_view=True, admin_folder=folder)

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    return app

app = create_app()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False)