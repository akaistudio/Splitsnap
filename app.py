"""SplitSnap — AI-powered group expense splitting"""
import os, json, uuid, secrets, base64, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
from io import BytesIO
from urllib.request import urlopen

import bcrypt, anthropic, psycopg2, psycopg2.extras
from flask import Flask, request, jsonify, redirect, session, render_template_string, send_file

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except: pass

try:
    import fitz  # PyMuPDF
except: fitz = None

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'splitsnap-prod-2026')
app.permanent_session_lifetime = timedelta(days=90)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=timedelta(days=90))

# Force print to flush immediately (for Railway logs)
import sys
sys.stdout.reconfigure(line_buffering=True)


@app.route('/debug')
def debug_info():
    info = {"status": "ok", "tables": [], "errors": []}
    try:
        conn = get_db()
        if not conn:
            info["errors"].append("No DATABASE_URL")
            return jsonify(info)
        cur = conn.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        info["tables"] = [r['table_name'] for r in cur.fetchall()]
        # Check each table
        for t in ['users', 'otp_codes', 'trips', 'trip_members', 'trip_expenses', 'settled_payments']:
            try:
                cur.execute(f"SELECT COUNT(*) as cnt FROM {t}")
                info[t] = cur.fetchone()['cnt']
            except Exception as e:
                info["errors"].append(f"{t}: {e}")
        # Check trips columns
        try:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='trips'")
            info["trips_columns"] = [r['column_name'] for r in cur.fetchall()]
        except: pass
        conn.close()
    except Exception as e:
        info["errors"].append(str(e))
    return jsonify(info)

@app.route('/debug-trips')
def debug_trips():
    """See raw trip data to debug loading issues"""
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM trips ORDER BY created_at DESC")
        trips = cur.fetchall()
        result = []
        for t in trips:
            trip = {k: str(v) if v is not None else None for k, v in dict(t).items()}
            cur.execute("SELECT name FROM trip_members WHERE trip_id=%s", (t['id'],))
            trip['members'] = [m['name'] for m in cur.fetchall()]
            cur.execute("SELECT id,description,amount,amount_base,currency,paid_by,split_among FROM trip_expenses WHERE trip_id=%s", (t['id'],))
            trip['expenses'] = [{k: str(v) for k, v in dict(e).items()} for e in cur.fetchall()]
        result.append(trip)
        conn.close()
        return jsonify({"trips": result})
    except Exception as e:
        return jsonify({"error": str(e)})

MODEL = os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-20250514')
UPLOAD_DIR = Path('/tmp/splitsnap_uploads'); UPLOAD_DIR.mkdir(exist_ok=True)

# ── Database ───────────────────────────────────────────────────
def get_db():
    url = os.environ.get('DATABASE_URL')
    if not url:
        print("⚠️ No DATABASE_URL set")
        return None
    # Railway sometimes gives postgres:// but psycopg2 needs postgresql://
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=5)
    conn.autocommit = True
    return conn

def init_db():
    conn = get_db()
    if not conn:
        print("⚠️ DATABASE_URL not set — skipping DB init")
        return
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT DEFAULT '',
        name TEXT DEFAULT '', currency TEXT DEFAULT 'EUR',
        is_superadmin BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW())""")
    cur.execute("""CREATE TABLE IF NOT EXISTS otp_codes (
        id SERIAL PRIMARY KEY, email TEXT NOT NULL, code TEXT NOT NULL,
        purpose TEXT DEFAULT 'login', used BOOLEAN DEFAULT FALSE,
        attempts INTEGER DEFAULT 0, expires_at TIMESTAMP NOT NULL,
        created_at TIMESTAMP DEFAULT NOW())""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_otp_email ON otp_codes(email, purpose, used)")
    cur.execute("""CREATE TABLE IF NOT EXISTS trips (
        id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL,
        currency VARCHAR(10) DEFAULT 'EUR', created_by INTEGER,
        settled BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW())""")
    cur.execute("""CREATE TABLE IF NOT EXISTS trip_members (
        id SERIAL PRIMARY KEY, trip_id VARCHAR(36) REFERENCES trips(id) ON DELETE CASCADE,
        name VARCHAR(255) NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS trip_expenses (
        id VARCHAR(36) PRIMARY KEY, trip_id VARCHAR(36) REFERENCES trips(id) ON DELETE CASCADE,
        description VARCHAR(255) NOT NULL, amount DOUBLE PRECISION DEFAULT 0,
        amount_base DOUBLE PRECISION DEFAULT 0, currency VARCHAR(10) DEFAULT 'EUR',
        paid_by VARCHAR(255) NOT NULL, split_among TEXT DEFAULT '[]',
        date VARCHAR(20), category VARCHAR(100) DEFAULT 'General',
        created_at TIMESTAMP DEFAULT NOW())""")
    cur.execute("""CREATE TABLE IF NOT EXISTS settled_payments (
        id SERIAL PRIMARY KEY, trip_id VARCHAR(36) REFERENCES trips(id) ON DELETE CASCADE,
        from_member TEXT NOT NULL, to_member TEXT NOT NULL, amount NUMERIC(12,2),
        settled_at TIMESTAMP DEFAULT NOW())""")
    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_superadmin BOOLEAN DEFAULT FALSE",
        "UPDATE users SET is_superadmin = TRUE WHERE id = (SELECT MIN(id) FROM users)",
        "ALTER TABLE users ALTER COLUMN password_hash SET DEFAULT ''",
        "ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL",
        """CREATE TABLE IF NOT EXISTS settled_payments (
            id SERIAL PRIMARY KEY, trip_id VARCHAR(36) REFERENCES trips(id) ON DELETE CASCADE,
            from_member TEXT NOT NULL, to_member TEXT NOT NULL, amount NUMERIC(12,2),
            settled_at TIMESTAMP DEFAULT NOW())""",
        "ALTER TABLE trips ADD COLUMN IF NOT EXISTS settled BOOLEAN DEFAULT FALSE",
    ]
    for m in migrations:
        try: cur.execute(m)
        except Exception as me: print(f"Migration note: {me}")
    conn.close()

try:
    init_db()
    print("✅ Database initialized")
except Exception as e:
    print(f"⚠️ Database init failed: {e}")

# ── Auth Helpers ───────────────────────────────────────────────
def hash_password(pw):
    return bcrypt.hashpw(pw.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(pw, hashed):
    try: return bcrypt.checkpw(pw.encode('utf-8'), hashed.encode('utf-8'))
    except: return False

def generate_otp():
    return ''.join([str(secrets.randbelow(10)) for _ in range(6)])

def send_otp_email(email, code, purpose='login'):
    resend_key = os.environ.get('RESEND_API_KEY', '')
    from_email = os.environ.get('SMTP_FROM', 'noreply@usevarnam.com')
    purpose_text = 'login' if purpose == 'login' else 'verification'
    subject = f"Your SplitSnap {purpose_text} code: {code}"
    html = f"""<div style="font-family:sans-serif;max-width:400px;margin:0 auto;padding:24px">
        <h2 style="color:#10b981">✂️ SplitSnap</h2>
        <p style="color:#666;font-size:14px">Your {purpose_text} code is:</p>
        <div style="font-size:36px;font-weight:800;letter-spacing:8px;color:#1a1a2e;text-align:center;
                    padding:20px;background:#f0fdf4;border-radius:12px;margin:16px 0">{code}</div>
        <p style="color:#999;font-size:12px">This code expires in 5 minutes. Do not share it.</p>
        <p style="color:#999;font-size:11px;margin-top:20px">Part of <a href="https://snapsuite.up.railway.app" style="color:#10b981">Varnam Suite</a></p>
    </div>"""

    if not resend_key:
        print(f"⚠️ RESEND_API_KEY not set. OTP for {email}: {code}")
        return False

    import requests as http_requests
    try:
        print(f"📤 Sending OTP to {email} via Resend...")
        r = http_requests.post('https://api.resend.com/emails', json={
            'from': from_email,
            'to': [email],
            'subject': subject,
            'html': html
        }, headers={'Authorization': f'Bearer {resend_key}'}, timeout=10)
        if r.status_code == 200:
            print(f"✅ OTP email sent to {email}")
            return True
        else:
            print(f"❌ Resend error {r.status_code}: {r.text}")
            print(f"💡 OTP for {email}: {code}")
            return False
    except Exception as e:
        print(f"❌ Email failed: {e}")
        print(f"💡 OTP for {email}: {code}")
        return False


# ── SSO (Varnam Suite single sign-on) ───────────────────────────
import hmac as _hmac, time as _time

VARNAM_SSO_SECRET = os.environ.get('VARNAM_SSO_SECRET', 'varnam-suite-sso-2026')
SSO_TOKEN_MAX_AGE = 300  # 5 minutes

def verify_sso_token(token):
    """Verify a token generated by FinanceSnap. Returns email or None."""
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        parts = decoded.rsplit('|', 2)
        if len(parts) != 3:
            return None
        email, timestamp, provided_sig = parts
        if _time.time() - int(timestamp) > SSO_TOKEN_MAX_AGE:
            return None
        payload = f"{email}|{timestamp}"
        expected_sig = _hmac.new(
            VARNAM_SSO_SECRET.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not _hmac.compare_digest(expected_sig, provided_sig):
            return None
        return email
    except Exception:
        return None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({"error": "Not logged in"}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

# ── Currency ───────────────────────────────────────────────────
_rate_cache = None; _rate_cache_time = None

def get_exchange_rates():
    global _rate_cache, _rate_cache_time
    if _rate_cache and _rate_cache_time and (datetime.now() - _rate_cache_time).seconds < 3600:
        return _rate_cache
    try:
        response = urlopen("https://open.er-api.com/v6/latest/USD", timeout=5)
        data = json.loads(response.read())
        _rate_cache = data.get('rates', {})
    except:
        _rate_cache = {'USD':1,'CAD':1.36,'EUR':0.92,'GBP':0.79,'INR':83.5,'AUD':1.53,'JPY':149.5,'CHF':0.88,'SGD':1.34,'AED':3.67,'MYR':4.45}
    _rate_cache_time = datetime.now()
    return _rate_cache

def convert_currency(amount, from_curr, to_curr):
    if from_curr == to_curr or amount == 0: return round(amount, 2)
    rates = get_exchange_rates()
    from_rate = rates.get(from_curr.upper(), 1)
    to_rate = rates.get(to_curr.upper(), 1)
    return round((amount / from_rate) * to_rate, 2)

# ── Receipt Scanner ────────────────────────────────────────────
def extract_receipt(image_list, media_type="image/jpeg"):
    client = anthropic.Anthropic()
    prompt = """Analyze this receipt and extract ALL information.
Return ONLY a valid JSON object:
{
  "date": "YYYY-MM-DD or empty", "vendor": "Business name",
  "category": "One of: Food & Dining, Groceries, Air Travel, Cab & Rideshare, Hotel & Accommodation, Shopping & Retail, Utilities, Entertainment, Other",
  "subtotal": 0.00, "tax": 0.00, "total": 0.00,
  "currency": "3-letter code e.g. EUR, USD, INR",
  "items": "List each item: 'Item (price), Item (price), ...'"
}
Return ONLY JSON, no other text."""
    content = []
    if isinstance(image_list, list):
        for img_bytes, mt in image_list:
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.standard_b64encode(img_bytes).decode("utf-8")}})
    else:
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(image_list).decode("utf-8")}})
    content.append({"type": "text", "text": prompt})
    response = client.messages.create(model=MODEL, max_tokens=1000, messages=[{"role": "user", "content": content}])
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    return json.loads(text)

# ── Routes: Pages ──────────────────────────────────────────────
@app.route('/')
def index():
    if 'user_id' in session: return redirect('/app')
    return redirect('/welcome')

@app.route('/demo')
@app.route('/demo/reset')
def demo_login():
    """One-click demo — shared account. Seed on first visit only."""
    force_reseed = request.path == '/demo/reset' and request.args.get('key') == 'varnam2026'
    if request.path == '/demo/reset' and not force_reseed:
        return redirect('/demo')
    demo_email = 'demo@varnam.app'
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE email=%s', (demo_email,))
    user = cur.fetchone()
    needs_seed = False
    if not user:
        cur.execute("INSERT INTO users (email, name, currency, is_superadmin) VALUES (%s,%s,%s,%s) RETURNING id",
                   (demo_email, 'Demo User', 'INR', True))
        user_id = cur.fetchone()['id']
        if not conn.autocommit: conn.commit()
        needs_seed = True
    else:
        user_id = user['id']
        if force_reseed:
            needs_seed = True
    if needs_seed:
        cur.execute("DELETE FROM trip_expenses WHERE trip_id IN (SELECT id FROM trips WHERE created_by=%s)", (user_id,))
        cur.execute("DELETE FROM trip_members WHERE trip_id IN (SELECT id FROM trips WHERE created_by=%s)", (user_id,))
        cur.execute("DELETE FROM settled_payments WHERE trip_id IN (SELECT id FROM trips WHERE created_by=%s)", (user_id,))
        cur.execute("DELETE FROM trips WHERE created_by=%s", (user_id,))
        # Seed a Goa trip
        import uuid as _uuid
        tid = str(_uuid.uuid4())
        cur.execute("INSERT INTO trips (id,name,currency,created_by) VALUES (%s,'Goa Trip Jan 2026','INR',%s)", (tid, user_id))
        members = ['Arjun','Sneha','Karthik','Priya']
        for m in members:
            cur.execute("INSERT INTO trip_members (trip_id,name) VALUES (%s,%s)", (tid,m))
        cur.execute("SELECT id,name FROM trip_members WHERE trip_id=%s", (tid,))
        mmap = {r['name']:r['id'] for r in cur.fetchall()}
        expenses = [
            ('Flight tickets','2026-01-10',12400,'Arjun',['Arjun','Sneha','Karthik','Priya']),
            ('Hotel Airbnb','2026-01-10',18000,'Sneha',['Arjun','Sneha','Karthik','Priya']),
            ('Scooter rental','2026-01-11',2400,'Karthik',['Arjun','Sneha','Karthik','Priya']),
            ('Beach shack dinner','2026-01-11',3200,'Priya',['Arjun','Sneha','Karthik','Priya']),
            ('Water sports','2026-01-12',5600,'Arjun',['Arjun','Sneha','Karthik']),
            ('Groceries & drinks','2026-01-12',1800,'Sneha',['Arjun','Sneha','Karthik','Priya']),
        ]
        for desc,date,amt,paid_by,split_among in expenses:
            eid = str(_uuid.uuid4())
            split_ids = ','.join(str(mmap[m]) for m in split_among if m in mmap)
            cur.execute("INSERT INTO trip_expenses (id,trip_id,description,amount,amount_base,currency,paid_by,split_among,date) VALUES (%s,%s,%s,%s,%s,'INR',%s,%s,%s)",
                       (eid,tid,desc,amt,amt,str(mmap.get(paid_by,0)),split_ids,date))
        if not conn.autocommit: conn.commit()
    session.clear()
    session['user_id'] = user_id
    session.permanent = True
    conn.close()
    return redirect('/')

@app.route('/welcome')
def welcome():
    return render_template_string(LANDING_HTML)


@app.route('/auto-login')
def auto_login():
    """SSO entry point — called by FinanceSnap with a signed token."""
    token = request.args.get('token', '')
    email = verify_sso_token(token)
    if not email:
        return redirect('/login')
    try:
        conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM users WHERE email=%s', (email,))
        user = cur.fetchone()
        if not user:
            name = email.split('@')[0]
            cur.execute(
                "INSERT INTO users (email, name, created_at) VALUES (%s,%s,NOW()) RETURNING id",
                (email, name)
            )
            row = cur.fetchone()
            user_id = row['id']
            if not conn.autocommit:
                conn.commit()
        else:
            user_id = user['id']
            name = user.get('name', '') if hasattr(user, 'get') else (user[2] if len(user) > 2 else '')
            name = name or email.split('@')[0]
        conn.close()
    except Exception as e:
        import traceback; print(f"SSO auto-login error for {email}: {e}\n{traceback.format_exc()}")
        return redirect('/login')
    session.clear()
    session.update({'user_id': user_id, 'user_name': name})
    session.permanent = True
    return redirect('/app')

@app.route('/login')
def login_page():
    if 'user_id' in session: return redirect('/app')
    return render_template_string(AUTH_HTML, show_register=False)

@app.route('/register')
def register_page():
    if 'user_id' in session: return redirect('/app')
    return render_template_string(AUTH_HTML, show_register=True)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/welcome')

@app.route('/app')
@login_required
def main_app():
    conn = get_db()
    is_admin = False
    is_demo = False
    if conn:
        cur = conn.cursor()
        cur.execute('SELECT is_superadmin, email FROM users WHERE id=%s', (session.get('user_id'),))
        u = cur.fetchone()
        is_admin = u.get('is_superadmin', False) if u else False
        is_demo = (u.get('email') == 'demo@varnam.app') if u else False
        conn.close()
    return render_template_string(TOOL_HTML, is_admin=is_admin, user_name=session.get('user_name', ''), is_demo=is_demo)

# ── Routes: OTP Auth ───────────────────────────────────────────
@app.route('/api/auth/send-otp', methods=['POST'])
def send_otp():
    data = request.get_json()
    email = (data.get('email') or '').strip().lower()
    purpose = data.get('purpose', 'login')
    print(f"📧 OTP request: {email} ({purpose})")
    if not email or '@' not in email:
        return jsonify({"error": "Valid email required"}), 400
    conn = get_db()
    if not conn: return jsonify({"error": "Database not configured"}), 500
    cur = conn.cursor()
    cur.execute("""SELECT COUNT(*) as cnt FROM otp_codes
                   WHERE email=%s AND created_at > NOW() - INTERVAL '15 minutes'""", (email,))
    if cur.fetchone()['cnt'] >= 5:
        conn.close(); return jsonify({"error": "Too many requests. Wait 15 minutes."}), 429
    if purpose == 'login':
        cur.execute('SELECT id FROM users WHERE email=%s', (email,))
        if not cur.fetchone():
            conn.close(); return jsonify({"error": "No account found with this email"}), 404
    if purpose == 'register':
        cur.execute('SELECT id FROM users WHERE email=%s', (email,))
        if cur.fetchone():
            conn.close(); return jsonify({"error": "Email already registered. Please sign in."}), 409
    cur.execute("UPDATE otp_codes SET used=TRUE WHERE email=%s AND purpose=%s AND used=FALSE", (email, purpose))
    code = generate_otp()
    expires = datetime.utcnow() + timedelta(minutes=5)
    cur.execute("INSERT INTO otp_codes (email, code, purpose, expires_at) VALUES (%s,%s,%s,%s)",
                (email, code, purpose, expires))
    conn.close()
    if send_otp_email(email, code, purpose):
        return jsonify({"success": True})
    # Email failed - return code directly so user can still proceed
    return jsonify({"success": True, "fallback_code": code, "email_failed": True})

@app.route('/api/auth/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json()
    email = (data.get('email') or '').strip().lower()
    code = (data.get('code') or '').strip()
    purpose = data.get('purpose', 'login')
    if not email or not code or len(code) != 6:
        return jsonify({"error": "Email and 6-digit code required"}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT * FROM otp_codes WHERE email=%s AND purpose=%s AND used=FALSE AND expires_at > NOW()
                   ORDER BY created_at DESC LIMIT 1""", (email, purpose))
    otp_rec = cur.fetchone()
    if not otp_rec:
        conn.close(); return jsonify({"error": "Code expired. Request a new one."}), 400
    if otp_rec['attempts'] >= 3:
        cur.execute("UPDATE otp_codes SET used=TRUE WHERE id=%s", (otp_rec['id'],))
        conn.close(); return jsonify({"error": "Too many attempts. Request a new code."}), 429
    cur.execute("UPDATE otp_codes SET attempts=attempts+1 WHERE id=%s", (otp_rec['id'],))
    if not secrets.compare_digest(code, otp_rec['code']):
        conn.close()
        remaining = 2 - otp_rec['attempts']
        return jsonify({"error": f"Invalid code. {remaining} attempt(s) remaining."}), 400
    cur.execute("UPDATE otp_codes SET used=TRUE WHERE id=%s", (otp_rec['id'],))
    if purpose == 'login':
        cur.execute('SELECT * FROM users WHERE email=%s', (email,))
        user = cur.fetchone(); conn.close()
        if user:
            session.update({'user_id': user['id'], 'user_name': user.get('name', email.split('@')[0])})
            session.permanent = True
            return jsonify({"success": True, "redirect": "/app"})
        return jsonify({"error": "User not found"}), 404
    conn.close()
    return jsonify({"success": True, "verified": True})

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.get_json()
    email = (data.get('email') or '').strip().lower()
    name = (data.get('name') or '').strip()
    currency = data.get('currency', 'EUR')
    code = (data.get('code') or '').strip()
    if not email or not code or len(code) != 6:
        return jsonify({"error": "Verification code required"}), 400
    if not name: return jsonify({"error": "Name is required"}), 400
    conn = get_db(); cur = conn.cursor()
    # Verify OTP
    cur.execute("""SELECT * FROM otp_codes WHERE email=%s AND purpose='register' AND used=FALSE AND expires_at > NOW()
                   ORDER BY created_at DESC LIMIT 1""", (email,))
    otp_rec = cur.fetchone()
    if not otp_rec: conn.close(); return jsonify({"error": "Code expired. Request a new one."}), 400
    if otp_rec['attempts'] >= 3:
        cur.execute("UPDATE otp_codes SET used=TRUE WHERE id=%s", (otp_rec['id'],)); conn.close()
        return jsonify({"error": "Too many attempts. Request a new code."}), 429
    cur.execute("UPDATE otp_codes SET attempts=attempts+1 WHERE id=%s", (otp_rec['id'],))
    if not secrets.compare_digest(code, otp_rec['code']):
        conn.close(); return jsonify({"error": f"Invalid code. {2 - otp_rec['attempts']} attempt(s) remaining."}), 400
    cur.execute("UPDATE otp_codes SET used=TRUE WHERE id=%s", (otp_rec['id'],))
    cur.execute('SELECT id FROM users WHERE email=%s', (email,))
    if cur.fetchone(): conn.close(); return jsonify({"error": "Email already registered"}), 409
    cur.execute("SELECT COUNT(*) as cnt FROM users"); count = cur.fetchone()['cnt']
    is_admin = (count == 0)
    cur.execute("INSERT INTO users (email, name, currency, is_superadmin) VALUES (%s,%s,%s,%s) RETURNING id",
                (email, name, currency, is_admin))
    user_id = cur.fetchone()['id']; conn.close()
    session.update({'user_id': user_id, 'user_name': name}); session.permanent = True
    register_with_hub(name, email, currency)
    return jsonify({"success": True, "redirect": "/app"})

# ── Routes: Trip Management ────────────────────────────────────
@app.route('/api/trips', methods=['GET'])
@login_required
def get_trips():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM trips WHERE created_by=%s ORDER BY created_at DESC", (session['user_id'],))
        trips = cur.fetchall()
        for t in trips:
            cur.execute("SELECT name FROM trip_members WHERE trip_id=%s ORDER BY id", (t['id'],))
            t['members'] = [m['name'] for m in cur.fetchall()]
            cur.execute("SELECT COALESCE(SUM(amount_base),0) as total FROM trip_expenses WHERE trip_id=%s", (t['id'],))
            t['total'] = float(cur.fetchone()['total'] or 0)
            cur.execute("SELECT COUNT(*) as cnt FROM trip_expenses WHERE trip_id=%s", (t['id'],))
            t['expense_count'] = cur.fetchone()['cnt']
        conn.close()
        return jsonify({"trips": [{**dict(t), 'created_at': str(t.get('created_at', ''))} for t in trips]})
    except Exception as e:
        print(f"ERROR in get_trips: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"trips": [], "error": str(e)}), 500

@app.route('/api/trips', methods=['POST'])
@login_required
def create_trip():
    data = request.json or {}
    if not data.get('name') or not data.get('members'):
        return jsonify({"error": "Trip name and at least 2 members required"}), 400
    members = [m.strip() for m in data['members'] if m.strip()]
    if len(members) < 2:
        return jsonify({"error": "Need at least 2 members"}), 400
    trip_id = str(uuid.uuid4())
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO trips (id,name,currency,created_by) VALUES (%s,%s,%s,%s)",
                (trip_id, data['name'], data.get('currency', 'EUR'), session['user_id']))
    for m in members:
        cur.execute("INSERT INTO trip_members (trip_id,name) VALUES (%s,%s)", (trip_id, m))
    conn.close()
    return jsonify({"success": True, "trip_id": trip_id})

@app.route('/api/trips/<trip_id>', methods=['DELETE'])
@login_required
def delete_trip(trip_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM trips WHERE id=%s AND created_by=%s", (trip_id, session['user_id']))
    conn.close()
    return jsonify({"success": True})

@app.route('/api/trips/<trip_id>/expenses', methods=['GET'])
@login_required
def get_trip_expenses(trip_id):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM trips WHERE id=%s", (trip_id,))
        trip = cur.fetchone()
        if not trip: conn.close(); return jsonify({"error": "Trip not found"}), 404
        cur.execute("SELECT name FROM trip_members WHERE trip_id=%s ORDER BY id", (trip_id,))
        members = [m['name'] for m in cur.fetchall()]
        cur.execute("SELECT * FROM trip_expenses WHERE trip_id=%s ORDER BY created_at DESC", (trip_id,))
        expenses = cur.fetchall()
        for e in expenses:
            try: e['split_among'] = json.loads(e['split_among']) if e['split_among'] else members
            except: e['split_among'] = members
            if not e.get('amount_base'):
                try: e['amount_base'] = convert_currency(float(e['amount']), e.get('currency', 'EUR'), trip['currency'])
                except: e['amount_base'] = float(e.get('amount', 0))

        # Calculate balances
        balances = {m: 0.0 for m in members}
        for e in expenses:
            amt = float(e.get('amount_base') or e.get('amount', 0))
            split_list = e.get('split_among') or members
            per_person = amt / len(split_list) if split_list else 0
            paid_by = e.get('paid_by', '')
            if paid_by in balances:
                balances[paid_by] += amt
            for p in split_list:
                if p in balances:
                    balances[p] -= per_person

        # Minimum settlements
        debtors = [(m, -b) for m, b in balances.items() if b < -0.01]
        creditors = [(m, b) for m, b in balances.items() if b > 0.01]
        debtors.sort(key=lambda x: -x[1]); creditors.sort(key=lambda x: -x[1])
        settlements = []; di, ci = 0, 0
        while di < len(debtors) and ci < len(creditors):
            debtor, debt = debtors[di]; creditor, credit = creditors[ci]
            amt = min(debt, credit)
            if amt > 0.01:
                settlements.append({"from": debtor, "to": creditor, "amount": round(amt, 2)})
            debtors[di] = (debtor, debt - amt); creditors[ci] = (creditor, credit - amt)
            if debtors[di][1] < 0.01: di += 1
            if creditors[ci][1] < 0.01: ci += 1
        # Get settled payments
        settled = []
        try:
            cur.execute("SELECT from_member,to_member,amount FROM settled_payments WHERE trip_id=%s", (trip_id,))
            settled = [{"from": s['from_member'], "to": s['to_member'], "amount": float(s['amount'])} for s in cur.fetchall()]
        except Exception as e:
            print(f"settled_payments query failed (table may not exist): {e}")
        conn.close()
        return jsonify({
            "trip": {**dict(trip), 'created_at': str(trip.get('created_at', ''))},
            "members": members,
            "expenses": [{**dict(e), 'created_at': str(e.get('created_at', '')), 'amount': float(e.get('amount', 0)), 'amount_base': float(e.get('amount_base') or e.get('amount', 0))} for e in expenses],
            "balances": {m: round(b, 2) for m, b in balances.items()},
            "settlements": settlements,
            "settled_payments": settled
        })
    except Exception as e:
        print(f"ERROR in get_trip_expenses: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e), "trip": {"name": "Error", "currency": "EUR", "created_at": ""}, "members": [], "expenses": [], "balances": {}, "settlements": [], "settled_payments": []}), 500

@app.route('/api/trips/<trip_id>/expenses', methods=['POST'])
@login_required
def add_trip_expense(trip_id):
    data = request.json or {}
    if not data.get('description') or not data.get('amount') or not data.get('paid_by'):
        return jsonify({"error": "Description, amount, and paid_by required"}), 400
    exp_id = str(uuid.uuid4())
    split_among = json.dumps(data.get('split_among', []))
    amount = float(data['amount'])
    exp_currency = data.get('currency', 'EUR').upper()
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT currency FROM trips WHERE id=%s", (trip_id,))
    trip = cur.fetchone()
    base_currency = trip['currency'] if trip else 'EUR'
    amount_base = convert_currency(amount, exp_currency, base_currency)
    cur.execute("""INSERT INTO trip_expenses (id,trip_id,description,amount,amount_base,currency,paid_by,split_among,date,category)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (exp_id, trip_id, data['description'], amount, amount_base,
                 exp_currency, data['paid_by'], split_among, data.get('date', ''), data.get('category', 'General')))
    conn.close()
    return jsonify({"success": True, "id": exp_id, "amount_base": amount_base})

@app.route('/api/trips/<trip_id>/expenses/<exp_id>', methods=['DELETE'])
@login_required
def delete_trip_expense(trip_id, exp_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM trip_expenses WHERE id=%s AND trip_id=%s", (exp_id, trip_id))
    conn.close()
    return jsonify({"success": True})

@app.route('/api/trips/<trip_id>/scan', methods=['POST'])
@login_required
def scan_trip_receipt(trip_id):
    if 'receipt' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files['receipt']
    paid_by = request.form.get('paid_by', '')
    split_among = request.form.get('split_among', '[]')
    try: split_among_list = json.loads(split_among)
    except: split_among_list = []

    ext = Path(file.filename).suffix.lower()
    ext_map = {'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.webp':'image/webp','.gif':'image/gif','.heic':'image/heic','.heif':'image/heic','.pdf':'application/pdf'}
    media_type = ext_map.get(ext, 'image/jpeg')
    image_bytes = file.read()

    if ext in ('.heic', '.heif'):
        try:
            from PIL import Image
            img = Image.open(BytesIO(image_bytes))
            buf = BytesIO(); img.convert('RGB').save(buf, format='JPEG', quality=85)
            image_bytes = buf.getvalue(); media_type = 'image/jpeg'
        except: pass

    if ext not in ('.pdf',) and len(image_bytes) > 1.5 * 1024 * 1024:
        try:
            from PIL import Image
            img = Image.open(BytesIO(image_bytes))
            if max(img.size) > 2000: img.thumbnail((2000, 2000), Image.LANCZOS)
            buf = BytesIO(); img.convert('RGB').save(buf, format='JPEG', quality=80)
            image_bytes = buf.getvalue(); media_type = 'image/jpeg'
        except: pass

    try:
        if ext == '.pdf' and fitz:
            pdf_doc = fitz.open(stream=image_bytes, filetype="pdf")
            page_images = []
            for i in range(min(len(pdf_doc), 5)):
                pix = pdf_doc[i].get_pixmap(dpi=200)
                page_images.append((pix.tobytes("png"), "image/png"))
            pdf_doc.close()
            data = extract_receipt(page_images)
        else:
            data = extract_receipt(image_bytes, media_type)
    except Exception as e:
        return jsonify({"error": f"Failed to scan: {str(e)}"}), 500

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT currency FROM trips WHERE id=%s", (trip_id,))
    trip = cur.fetchone()
    if not trip: conn.close(); return jsonify({"error": "Trip not found"}), 404

    amount = float(data.get('total', 0))
    exp_currency = data.get('currency', 'EUR').upper()
    amount_base = convert_currency(amount, exp_currency, trip['currency'])
    vendor = data.get('vendor', 'Unknown')
    exp_id = str(uuid.uuid4())

    if not split_among_list:
        cur.execute("SELECT name FROM trip_members WHERE trip_id=%s ORDER BY id", (trip_id,))
        split_among_list = [m['name'] for m in cur.fetchall()]

    cur.execute("""INSERT INTO trip_expenses (id,trip_id,description,amount,amount_base,currency,paid_by,split_among,date,category)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (exp_id, trip_id, vendor, amount, amount_base, exp_currency,
                 paid_by, json.dumps(split_among_list), data.get('date', ''), data.get('category', 'Other')))
    conn.close()
    return jsonify({"success": True, "id": exp_id, "expense": {
        "description": vendor, "amount": amount, "amount_base": amount_base,
        "currency": exp_currency, "paid_by": paid_by, "category": data.get('category', 'Other'),
        "date": data.get('date', ''), "items": data.get('items', '')}})

# ── Settlement Tracking ────────────────────────────────────────
@app.route('/api/trips/<trip_id>/settle', methods=['POST'])
@login_required
def settle_payment(trip_id):
    data = request.json or {}
    from_m = data.get('from', '').strip()
    to_m = data.get('to', '').strip()
    amount = float(data.get('amount', 0))
    if not from_m or not to_m or amount <= 0:
        return jsonify({"error": "Invalid settlement"}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO settled_payments (trip_id,from_member,to_member,amount) VALUES (%s,%s,%s,%s)",
                (trip_id, from_m, to_m, amount))
    conn.close()
    return jsonify({"success": True})

@app.route('/api/trips/<trip_id>/unsettle', methods=['POST'])
@login_required
def unsettle_payment(trip_id):
    data = request.json or {}
    from_m = data.get('from', '').strip()
    to_m = data.get('to', '').strip()
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM settled_payments WHERE trip_id=%s AND from_member=%s AND to_member=%s",
                (trip_id, from_m, to_m))
    conn.close()
    return jsonify({"success": True})

@app.route('/api/trips/<trip_id>/settle-all', methods=['POST'])
@login_required
def settle_all(trip_id):
    """Mark all outstanding settlements as paid"""
    conn = get_db(); cur = conn.cursor()
    # Get current settlements
    cur.execute("SELECT * FROM trips WHERE id=%s", (trip_id,))
    trip = cur.fetchone()
    if not trip: conn.close(); return jsonify({"error": "Not found"}), 404
    cur.execute("SELECT name FROM trip_members WHERE trip_id=%s ORDER BY id", (trip_id,))
    members = [m['name'] for m in cur.fetchall()]
    cur.execute("SELECT * FROM trip_expenses WHERE trip_id=%s", (trip_id,))
    expenses = cur.fetchall()
    # Recalculate
    balances = {m: 0.0 for m in members}
    for e in expenses:
        amt = float(e.get('amount_base') or e['amount'])
        split_list = json.loads(e['split_among']) if e['split_among'] else members
        per_person = amt / len(split_list) if split_list else 0
        balances[e['paid_by']] = balances.get(e['paid_by'], 0) + amt
        for p in split_list:
            balances[p] = balances.get(p, 0) - per_person
    debtors = [(m, -b) for m, b in balances.items() if b < -0.01]
    creditors = [(m, b) for m, b in balances.items() if b > 0.01]
    debtors.sort(key=lambda x: -x[1]); creditors.sort(key=lambda x: -x[1])
    di, ci = 0, 0
    while di < len(debtors) and ci < len(creditors):
        debtor, debt = debtors[di]; creditor, credit = creditors[ci]
        amt = min(debt, credit)
        if amt > 0.01:
            cur.execute("""INSERT INTO settled_payments (trip_id,from_member,to_member,amount)
                          SELECT %s,%s,%s,%s WHERE NOT EXISTS
                          (SELECT 1 FROM settled_payments WHERE trip_id=%s AND from_member=%s AND to_member=%s)""",
                       (trip_id, debtor, creditor, round(amt,2), trip_id, debtor, creditor))
        debtors[di] = (debtor, debt - amt); creditors[ci] = (creditor, credit - amt)
        if debtors[di][1] < 0.01: di += 1
        if creditors[ci][1] < 0.01: ci += 1
    cur.execute("UPDATE trips SET settled=TRUE WHERE id=%s", (trip_id,))
    conn.close()
    return jsonify({"success": True})

@app.route('/api/trips/<trip_id>/reset-settlements', methods=['POST'])
@login_required
def reset_settlements(trip_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM settled_payments WHERE trip_id=%s", (trip_id,))
    cur.execute("UPDATE trips SET settled=FALSE WHERE id=%s", (trip_id,))
    conn.close()
    return jsonify({"success": True})

# ── FinanceSnap Integration ────────────────────────────────────
def register_with_hub(user_name, email, currency):
    try:
        import requests as http_requests
        hub = os.environ.get('FINANCESNAP_URL', 'https://snapsuite.up.railway.app')
        http_requests.post(f'{hub}/api/register-company', json={
            'app_name': 'SplitSnap', 'company_name': user_name,
            'email': email, 'currency': currency,
            'app_url': os.environ.get('SPLITSNAP_URL', 'https://splitsnap.up.railway.app')
        }, timeout=5)
    except: pass

@app.route('/api/trips/summary')
def api_trips_summary():
    """External API for FinanceSnap — returns trip spending summary per user"""
    api_key = request.headers.get('X-API-Key', '')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    conn = get_db()
    if not conn:
        return jsonify({'error': 'DB unavailable'}), 500
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM users WHERE email=%s', (api_key,))
    user = cur.fetchone()
    if not user:
        conn.close()
        return jsonify({'error': 'Invalid API key'}), 401

    # Return all trips this user created with totals
    cur.execute("""
        SELECT t.id, t.name, t.currency, t.settled, t.created_at,
               COALESCE(SUM(e.amount_base), 0) as total_amount,
               COUNT(e.id) as expense_count,
               COUNT(DISTINCT m.id) as member_count
        FROM trips t
        LEFT JOIN trip_expenses e ON e.trip_id = t.id
        LEFT JOIN trip_members m ON m.trip_id = t.id
        WHERE t.created_by = %s
        GROUP BY t.id, t.name, t.currency, t.settled, t.created_at
        ORDER BY t.created_at DESC
    """, (user['id'],))
    trips = cur.fetchall()
    conn.close()

    result = []
    for t in trips:
        d = dict(t)
        for k, v in d.items():
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
        result.append(d)

    total_spent = sum(float(t.get('total_amount', 0) or 0) for t in result)
    return jsonify({'trips': result, 'count': len(result), 'total_spent': total_spent})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'app': 'SplitSnap'})

# ── Admin Dashboard ────────────────────────────────────────────
@app.route('/admin')
@login_required
def admin():
    conn = get_db()
    if not conn: return redirect('/app')
    cur = conn.cursor()
    cur.execute('SELECT is_superadmin FROM users WHERE id=%s', (session['user_id'],))
    u = cur.fetchone()
    if not u or not u.get('is_superadmin'): conn.close(); return redirect('/app')
    cur.execute("SELECT COUNT(*) as c FROM users"); total_users = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) as c FROM trips"); total_trips = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) as c FROM trip_expenses"); total_expenses = cur.fetchone()['c']
    cur.execute("SELECT * FROM users ORDER BY created_at DESC")
    users = cur.fetchall()
    for u in users:
        cur.execute("SELECT COUNT(*) as c FROM trips WHERE created_by=%s", (u['id'],))
        u['trip_count'] = cur.fetchone()['c']
    conn.close()
    return render_template_string(ADMIN_HTML, total_users=total_users, total_trips=total_trips,
                                  total_expenses=total_expenses, users=users)

# ══════════════════════════════════════════════════════════════
# HTML TEMPLATES
# ══════════════════════════════════════════════════════════════

LANDING_HTML = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SplitSnap — AI Group Expense Splitter</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0B0F1A;--surface:#141926;--border:#2A3148;--text:#E8ECF4;--text2:#8B95B0;--accent:#10b981;--accent2:#34d399;--purple:#6C5CE7}
*{margin:0;padding:0;box-sizing:border-box}body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text)}
a{color:inherit;text-decoration:none}
</style></head><body>
<div style="position:fixed;top:0;left:0;right:0;z-index:100;padding:12px 24px;display:flex;align-items:center;justify-content:space-between;background:rgba(11,15,26,0.9);backdrop-filter:blur(12px);border-bottom:1px solid rgba(255,255,255,0.06)"><a href="https://snapsuite.up.railway.app" style="font-size:13px;color:#8B95B0;text-decoration:none;font-weight:600">← Varnam Suite</a><a href="/login" style="font-size:13px;color:#fff;text-decoration:none;font-weight:700;padding:8px 18px;background:var(--accent);border-radius:8px">Sign In</a></div>
<div style="padding:16px 24px;display:flex;justify-content:space-between;align-items:center;max-width:1200px;margin:0 auto">
<div style="display:flex;align-items:center;gap:10px"><div style="width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#10b981,#059669);display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:800;color:#fff">S</div><span style="font-size:18px;font-weight:800;color:#fff">Split<span style="color:var(--accent)">Snap</span></span></div>
<div style="display:flex;gap:12px"><a href="/login" style="padding:10px 20px;border:1.5px solid var(--border);border-radius:10px;font-size:14px;font-weight:600;color:var(--text2)">Sign In</a><a href="/register" style="padding:10px 20px;background:linear-gradient(135deg,#10b981,#059669);border-radius:10px;font-size:14px;font-weight:700;color:#fff">Get Started</a></div>
</div>
<section style="padding:80px 24px 60px;text-align:center;max-width:700px;margin:0 auto">
<div style="display:inline-block;padding:6px 16px;background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.2);border-radius:20px;font-size:12px;font-weight:700;color:var(--accent);margin-bottom:20px">PART OF SNAPSUITE</div>
<h1 style="font-size:clamp(32px,5vw,48px);font-weight:800;line-height:1.15;margin-bottom:16px;color:#fff">Split bills.<br><span style="background:linear-gradient(135deg,var(--accent),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent">Settle smart.</span></h1>
<p style="font-size:17px;color:var(--text2);line-height:1.7;margin-bottom:28px;max-width:560px;margin-left:auto;margin-right:auto">Like Splitwise — but with AI receipt scanning. Create a trip, add friends, snap receipts or add expenses manually. Auto-calculates who owes whom with minimum settlements. Multi-currency.</p>
<a href="/register" style="display:inline-block;padding:16px 40px;background:linear-gradient(135deg,#10b981,#059669);color:#fff;border-radius:12px;font-size:16px;font-weight:700;box-shadow:0 4px 20px rgba(16,185,129,.3);margin-right:12px">Start Splitting →</a><a href="/demo" style="display:inline-block;padding:16px 40px;background:transparent;color:#10b981;border:1.5px solid #10b981;border-radius:12px;font-size:16px;font-weight:600">Try Demo →</a>
</section>
<section style="padding:40px 24px;max-width:900px;margin:0 auto">
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px">
<div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px"><div style="font-size:26px;margin-bottom:10px">📸</div><div style="font-size:15px;font-weight:700;color:#fff;margin-bottom:6px">AI Receipt Scanner</div><div style="font-size:13px;color:var(--text2);line-height:1.6">Snap a photo of any receipt — AI extracts vendor, items, amount, currency. Supports multi-page PDFs and HEIC photos.</div></div>
<div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px"><div style="font-size:26px;margin-bottom:10px">💸</div><div style="font-size:15px;font-weight:700;color:#fff;margin-bottom:6px">Smart Settlements</div><div style="font-size:13px;color:var(--text2);line-height:1.6">Minimum number of transfers to settle up. No back-and-forth — just clear arrows showing who pays whom.</div></div>
<div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px"><div style="font-size:26px;margin-bottom:10px">💱</div><div style="font-size:15px;font-weight:700;color:#fff;margin-bottom:6px">Multi-Currency</div><div style="font-size:13px;color:var(--text2);line-height:1.6">Mix EUR, USD, GBP, INR — expenses auto-convert to your trip's base currency for settlements.</div></div>
<div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px"><div style="font-size:26px;margin-bottom:10px">📊</div><div style="font-size:15px;font-weight:700;color:#fff;margin-bottom:6px">Live Balance Bars</div><div style="font-size:13px;color:var(--text2);line-height:1.6">See at a glance who's overspent and who's underspent. Visual balance bars update instantly.</div></div>
<div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px"><div style="font-size:26px;margin-bottom:10px">✂️</div><div style="font-size:15px;font-weight:700;color:#fff;margin-bottom:6px">Custom Splits</div><div style="font-size:13px;color:var(--text2);line-height:1.6">Split equally among everyone, or select specific people. Paid by one, shared by some.</div></div>
<div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px"><div style="font-size:26px;margin-bottom:10px">🔒</div><div style="font-size:15px;font-weight:700;color:#fff;margin-bottom:6px">Secure OTP Login</div><div style="font-size:13px;color:var(--text2);line-height:1.6">No passwords to remember. Sign in with a 6-digit code sent to your email. Secure and simple.</div></div>
</div></section>
<div style="padding:40px 24px 80px;text-align:center;font-size:13px;color:var(--text2)">SplitSnap · Part of <a href="https://snapsuite.up.railway.app" style="color:var(--accent)">Varnam Suite</a> · Built with Claude AI · Powered by Shakty.AI</div>
</body></html>"""

AUTH_HTML = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SplitSnap — {% if show_register %}Create Account{% else %}Sign In{% endif %}</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0B0F1A;--surface:#141926;--border:#2A3148;--text:#E8ECF4;--text2:#8B95B0;--accent:#10b981;--accent2:#34d399;--green:#4ADE80;--red:#F87171}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center}
.container{width:100%;max-width:440px;padding:24px}
.logo{text-align:center;margin-bottom:32px}
.logo .icon{width:56px;height:56px;border-radius:14px;display:inline-flex;align-items:center;justify-content:center;font-size:24px;font-weight:800;color:#fff;background:linear-gradient(135deg,#10b981,#059669);margin-bottom:12px}
.logo h1{font-size:28px;font-weight:800;color:#fff}.logo h1 span{color:var(--accent)}
.logo p{font-size:14px;color:var(--text2);margin-top:6px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:32px}
.card h2{font-size:20px;font-weight:700;margin-bottom:6px;color:#fff}
.card .sub{font-size:13px;color:var(--text2);margin-bottom:20px}
.fg{margin-bottom:16px}
.fg label{display:block;font-size:12px;font-weight:700;color:var(--text2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.fg input,.fg select{width:100%;padding:12px 14px;background:var(--bg);border:1.5px solid var(--border);border-radius:10px;color:#fff;font-size:14px;font-family:'DM Sans',sans-serif}
.fg input:focus,.fg select:focus{outline:none;border-color:var(--accent)}
.otp-row{display:flex;gap:8px;justify-content:center;margin-bottom:16px}
.otp-row input{width:48px;height:56px;text-align:center;font-size:24px;font-weight:800;background:var(--bg);border:1.5px solid var(--border);border-radius:10px;color:#fff;font-family:'DM Sans',sans-serif;outline:none;transition:.2s}
.otp-row input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(16,185,129,.15)}
.btn{width:100%;padding:14px;border:none;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer;font-family:'DM Sans',sans-serif;background:linear-gradient(135deg,#10b981,#059669);color:#fff;transition:.2s}
.btn:hover{opacity:.9;transform:translateY(-1px)}.btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.switch{text-align:center;margin-top:16px;font-size:14px;color:var(--text2)}.switch a{color:var(--accent);text-decoration:none;font-weight:600;cursor:pointer}
.flash{padding:12px;border-radius:10px;font-size:13px;margin-bottom:16px}
.flash.error{background:rgba(248,113,113,.1);color:var(--red);border:1px solid rgba(248,113,113,.2)}
.flash.success{background:rgba(74,222,128,.1);color:var(--green);border:1px solid rgba(74,222,128,.2)}
.hidden{display:none}
.timer{text-align:center;font-size:13px;color:var(--text2);margin-top:10px}
.timer a{color:var(--accent);cursor:pointer;text-decoration:none;font-weight:600}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
.lock-icon{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text2);margin-top:12px;justify-content:center}
.suite{margin-top:24px;text-align:center;font-size:12px;color:var(--text2)}.suite a{color:var(--text2);text-decoration:none;font-weight:600}.suite a:hover{color:var(--accent)}
</style></head><body>
<div class="container">
<div class="logo"><div class="icon">S</div><h1>Split<span>Snap</span></h1><p>AI-powered group expense splitting</p></div>
<div id="alertBox"></div>
{% if show_register %}
<div id="regStep1" class="card">
<h2>Create Account</h2><p class="sub">We'll send a 6-digit code to verify your email</p>
<div class="fg"><label>Your Name</label><input type="text" id="regName" required placeholder="Your name"></div>
<div class="fg"><label>Email</label><input type="email" id="regEmail" required placeholder="you@email.com"></div>
<div class="fg"><label>Default Currency</label><select id="regCurrency"><option value="EUR" selected>🇪🇺 EUR</option><option value="USD">🇺🇸 USD</option><option value="GBP">🇬🇧 GBP</option><option value="INR">🇮🇳 INR</option><option value="CAD">🇨🇦 CAD</option><option value="MYR">🇲🇾 MYR</option><option value="SGD">🇸🇬 SGD</option><option value="AUD">🇦🇺 AUD</option><option value="AED">🇦🇪 AED</option></select></div>
<button class="btn" id="regSendBtn" onclick="sendRegOTP()">Send Verification Code</button>
<div class="switch">Already have an account? <a href="/login">Sign in</a></div>
</div>
<div id="regStep2" class="card hidden">
<h2>Verify Email</h2><p class="sub">Enter the 6-digit code sent to <strong id="regEmailDisplay"></strong></p>
<div class="otp-row" id="regOtpRow"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"></div>
<button class="btn" id="regVerifyBtn" onclick="verifyRegOTP()">Create Account</button>
<div class="timer" id="regTimer">Resend code in <span id="regCountdown">60</span>s</div>
<div class="timer hidden" id="regResend"><a onclick="sendRegOTP()">Resend code</a> · <a onclick="showStep('regStep1')">Change email</a></div>
</div>
{% else %}
<div id="loginStep1" class="card">
<h2>Sign In</h2><p class="sub">Enter your email to receive a 6-digit login code</p>
<div class="fg"><label>Email</label><input type="email" id="loginEmail" required placeholder="you@email.com" onkeydown="if(event.key==='Enter')sendLoginOTP()"></div>
<button class="btn" id="loginSendBtn" onclick="sendLoginOTP()">Send Login Code</button>
<div class="switch">New here? <a href="/register">Create account</a></div>
</div>
<div id="loginStep2" class="card hidden">
<h2>Enter Code</h2><p class="sub">Enter the 6-digit code sent to <strong id="loginEmailDisplay"></strong></p>
<div class="otp-row" id="loginOtpRow"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"><input type="text" maxlength="1" class="otp-digit" inputmode="numeric"></div>
<button class="btn" id="loginVerifyBtn" onclick="verifyLoginOTP()">Sign In</button>
<div class="timer" id="loginTimer">Resend code in <span id="loginCountdown">60</span>s</div>
<div class="timer hidden" id="loginResend"><a onclick="sendLoginOTP()">Resend code</a> · <a onclick="showStep('loginStep1')">Change email</a></div>
</div>
{% endif %}
<div class="suite"><a href="/welcome">← Back to SplitSnap home</a></div>
<div class="lock-icon">🔒 Secured with email OTP verification</div>
</div>
<script>
document.querySelectorAll('.otp-row').forEach(row=>{const inputs=row.querySelectorAll('.otp-digit');inputs.forEach((inp,i)=>{inp.addEventListener('input',e=>{const v=e.target.value.replace(/\\D/g,'');e.target.value=v.charAt(0)||'';if(v&&i<5)inputs[i+1].focus();});inp.addEventListener('keydown',e=>{if(e.key==='Backspace'&&!e.target.value&&i>0){inputs[i-1].focus();inputs[i-1].value='';}});inp.addEventListener('paste',e=>{e.preventDefault();const p=(e.clipboardData.getData('text')||'').replace(/\\D/g,'').slice(0,6);p.split('').forEach((c,j)=>{if(inputs[j])inputs[j].value=c;});if(p.length>=6)inputs[5].focus();});});});
function getOTP(rowId){return Array.from(document.getElementById(rowId).querySelectorAll('.otp-digit')).map(i=>i.value).join('');}
function clearOTP(rowId){document.getElementById(rowId).querySelectorAll('.otp-digit').forEach(i=>i.value='');}
function showAlert(msg,type){document.getElementById('alertBox').innerHTML='<div class="flash '+type+'">'+msg+'</div>';}
function showStep(id){document.querySelectorAll('.card').forEach(c=>c.classList.add('hidden'));document.getElementById(id).classList.remove('hidden');}
function startTimer(cntId,timerId,resendId,sec){let r=sec;const el=document.getElementById(cntId);document.getElementById(timerId).classList.remove('hidden');document.getElementById(resendId).classList.add('hidden');el.textContent=r;const iv=setInterval(()=>{r--;el.textContent=r;if(r<=0){clearInterval(iv);document.getElementById(timerId).classList.add('hidden');document.getElementById(resendId).classList.remove('hidden');}},1000);}
let loginEmailVal='',regData={};
async function sendLoginOTP(){const email=document.getElementById('loginEmail').value.trim().toLowerCase();if(!email){showAlert('Enter email','error');return;}loginEmailVal=email;const btn=document.getElementById('loginSendBtn');btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Sending...';try{const r=await fetch('/api/auth/send-otp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,purpose:'login'})});const d=await r.json();if(d.success){document.getElementById('loginEmailDisplay').textContent=email;showStep('loginStep2');if(d.fallback_code){showAlert('Email delivery unavailable. Your code: '+d.fallback_code,'success');}else{showAlert('Code sent to '+email,'success');}startTimer('loginCountdown','loginTimer','loginResend',60);setTimeout(()=>document.querySelector('#loginStep2 .otp-digit').focus(),100);}else{showAlert(d.error||'Failed','error');}}catch(e){showAlert('Connection error','error');}btn.disabled=false;btn.innerHTML='Send Login Code';}
async function verifyLoginOTP(){const code=getOTP('loginOtpRow');if(code.length!==6){showAlert('Enter the full 6-digit code','error');return;}const btn=document.getElementById('loginVerifyBtn');btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Verifying...';try{const r=await fetch('/api/auth/verify-otp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:loginEmailVal,code,purpose:'login'})});const d=await r.json();if(d.success){window.location.href=d.redirect||'/app';}else{showAlert(d.error||'Invalid code','error');clearOTP('loginOtpRow');}}catch(e){showAlert('Connection error','error');}btn.disabled=false;btn.innerHTML='Sign In';}
async function sendRegOTP(){const name=document.getElementById('regName').value.trim(),email=document.getElementById('regEmail').value.trim().toLowerCase(),cur=document.getElementById('regCurrency').value;if(!name){showAlert('Enter name','error');return;}if(!email){showAlert('Enter email','error');return;}regData={name,email,currency:cur};const btn=document.getElementById('regSendBtn');btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Sending...';try{const r=await fetch('/api/auth/send-otp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,purpose:'register'})});const d=await r.json();if(d.success){document.getElementById('regEmailDisplay').textContent=email;showStep('regStep2');if(d.fallback_code){showAlert('Email delivery unavailable. Your code: '+d.fallback_code,'success');}else{showAlert('Code sent to '+email,'success');}startTimer('regCountdown','regTimer','regResend',60);setTimeout(()=>document.querySelector('#regStep2 .otp-digit').focus(),100);}else{showAlert(d.error||'Failed','error');}}catch(e){showAlert('Connection error','error');}btn.disabled=false;btn.innerHTML='Send Verification Code';}
async function verifyRegOTP(){const code=getOTP('regOtpRow');if(code.length!==6){showAlert('Enter the full 6-digit code','error');return;}const btn=document.getElementById('regVerifyBtn');btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Creating...';try{const r=await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...regData,code})});const d=await r.json();if(d.success){window.location.href=d.redirect||'/app';}else{showAlert(d.error||'Failed','error');clearOTP('regOtpRow');}}catch(e){showAlert('Connection error','error');}btn.disabled=false;btn.innerHTML='Create Account';}
</script></body></html>"""

TOOL_HTML = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SplitSnap — Your Trips</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0B0F1A;--surface:#141926;--card:#1B2133;--border:#2A3148;--text1:#E8ECF4;--text2:#8B95B0;--accent:#10b981;--accent2:#34d399;--red:#F87171;--purple:#6C5CE7}
*{margin:0;padding:0;box-sizing:border-box}body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text1);min-height:100vh}
.hdr{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--border)}
.hdr h1{font-size:20px;font-weight:800;color:#fff}.hdr h1 span{color:var(--accent)}
.hdr-links{display:flex;gap:12px;font-size:13px;color:var(--text2)}
.hdr-links a{color:var(--text2);text-decoration:none;font-weight:600}.hdr-links a:hover{color:var(--accent)}
.content{max-width:700px;margin:0 auto;padding:20px}
.card{background:var(--card);border:1.5px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px}
.btn{padding:10px 20px;border:none;border-radius:10px;font-weight:700;font-size:13px;cursor:pointer;font-family:'DM Sans',sans-serif;transition:.2s}
.btn-primary{background:linear-gradient(135deg,#10b981,#059669);color:#fff}
.btn-primary:hover{opacity:.9}.btn-primary:disabled{opacity:.5;cursor:not-allowed}
.btn-ghost{background:transparent;border:1.5px solid var(--border);color:var(--text2)}
.btn-ghost:hover{border-color:var(--accent);color:var(--accent)}
.btn-sm{padding:8px 14px;font-size:12px}
.btn-danger{background:rgba(248,113,113,.1);color:var(--red);border:1px solid rgba(248,113,113,.2)}
input,select,textarea{width:100%;padding:10px 12px;background:var(--bg);border:1.5px solid var(--border);border-radius:8px;color:#fff;font-size:14px;font-family:'DM Sans',sans-serif}
input:focus,select:focus{outline:none;border-color:var(--accent)}
label{font-size:12px;font-weight:600;color:var(--text2);display:block;margin-bottom:4px}
.trip-card{background:var(--card);border:1.5px solid var(--border);border-radius:14px;padding:18px;margin-bottom:12px;cursor:pointer;transition:.2s}
.trip-card:hover{border-color:var(--accent)}
.hidden{display:none}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
.upload-zone{border:2px dashed var(--border);border-radius:12px;padding:24px;text-align:center;cursor:pointer;transition:.2s;position:relative}
.upload-zone:hover{border-color:var(--accent);background:rgba(16,185,129,.03)}
.upload-zone input{position:absolute;inset:0;opacity:0;cursor:pointer}
.settlement-arrow{display:flex;align-items:center;gap:8px;padding:10px 12px;background:rgba(16,185,129,.05);border-radius:8px;margin-bottom:6px;font-size:13px;flex-wrap:wrap}
.balance-bar{margin-bottom:8px}
.balance-bar .bar{height:8px;border-radius:4px;transition:width .3s}

@media(max-width:768px){
  .hdr{flex-wrap:wrap;gap:6px;padding:10px 14px}
  .hdr-links{flex-wrap:wrap;gap:4px}
  .content{padding:12px}
  .card{padding:14px}
  #newTripForm div[style*="grid-template-columns:1fr 1fr"]{display:grid!important;grid-template-columns:1fr!important}
  .stats{grid-template-columns:repeat(2,1fr)!important}
  input,select,textarea{font-size:16px!important}
  .settlement-arrow{font-size:12px}
  .btn{padding:8px 14px;font-size:13px}
  h2{font-size:16px!important}
}
</style></head><body>
{% if is_demo %}
<div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);padding:10px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px;font-weight:600;color:#fff;position:sticky;top:0;z-index:200;flex-wrap:wrap">
  <span>🎭 Demo mode — Bloom Studio &nbsp;·&nbsp; <span style="font-weight:400;opacity:.85">Explore freely, nothing is saved permanently</span></span>
  <div style="display:flex;gap:8px;flex-shrink:0">
    <a href="/demo/reset?key=varnam2026" style="padding:6px 14px;background:rgba(255,255,255,0.15);color:#fff;border-radius:6px;text-decoration:none;font-size:12px;font-weight:700">↺ Reset Demo</a>
    <a href="/register" style="padding:6px 14px;background:#fff;color:#4f46e5;border-radius:6px;text-decoration:none;font-size:12px;font-weight:700">Create Account →</a>
  </div>
</div>
{% endif %}
<div class="hdr">
<h1>✂️ Split<span>Snap</span></h1>
<div class="hdr-links">
{% if is_admin %}<a href="/admin">Admin</a>{% endif %}
<a href="/logout">Sign Out</a>
</div>
</div>
<div class="content">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
<div><h2 style="font-size:18px;font-weight:700">Your Trips</h2><p style="font-size:13px;color:var(--text2)">Hi {{ user_name }} — create a trip and start splitting</p></div>
<button class="btn btn-primary btn-sm" onclick="showNewTrip()">+ New Trip</button>
</div>

<div id="newTripForm" class="card hidden">
<h4 style="margin-bottom:14px">Create New Trip</h4>
<div style="display:grid;gap:10px">
<div><label>Trip Name *</label><input type="text" id="tripName" placeholder="e.g. Ireland & Scotland 2026"></div>
<div><label>Members * (comma separated)</label><input type="text" id="tripMembers" placeholder="e.g. Priya, Sarah, Mei, Lisa"></div>
<div><label>Settle in (base currency)</label>
<select id="tripCurrency"><option value="EUR" selected>🇪🇺 EUR</option><option value="USD">🇺🇸 USD</option><option value="GBP">🇬🇧 GBP</option><option value="INR">🇮🇳 INR</option><option value="CAD">🇨🇦 CAD</option><option value="MYR">🇲🇾 MYR</option><option value="AUD">🇦🇺 AUD</option><option value="SGD">🇸🇬 SGD</option><option value="AED">🇦🇪 AED</option><option value="JPY">🇯🇵 JPY</option></select>
<div style="font-size:11px;color:var(--text2);margin-top:4px">All expenses converted to this for settlements</div></div>
<div style="display:flex;gap:10px;justify-content:flex-end">
<button class="btn btn-ghost btn-sm" onclick="hideNewTrip()">Cancel</button>
<button class="btn btn-primary btn-sm" onclick="createTrip()">Create Trip</button></div>
</div></div>

<div id="tripList"><div style="text-align:center;padding:40px;color:var(--text2)"><span class="spinner"></span> Loading trips...</div></div>

<div id="tripDetail" class="hidden">
<div style="display:flex;align-items:center;gap:12px;margin-bottom:20px">
<button class="btn btn-ghost btn-sm" onclick="backToList()">← Back</button>
<h3 id="detailName" style="font-size:18px;font-weight:700"></h3>
</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
<div class="card"><div style="font-size:12px;font-weight:600;color:var(--text2);text-transform:uppercase">Total Spent</div><div id="detailTotal" style="font-size:24px;font-weight:700;color:var(--accent2);margin-top:4px"></div></div>
<div class="card"><div style="font-size:12px;font-weight:600;color:var(--text2);text-transform:uppercase">Per Person</div><div id="detailPerPerson" style="font-size:24px;font-weight:700;color:var(--text1);margin-top:4px"></div></div>
</div>
<div id="settlementsBox" style="background:linear-gradient(135deg,#1a1a2e,#16213e);border:1.5px solid var(--accent);border-radius:14px;padding:16px;margin-bottom:16px">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
<div style="font-size:13px;font-weight:700;color:var(--accent2)">💸 Settle Up</div>
<div id="settleActions"></div>
</div>
<div id="settlementsList"></div>
</div>
<div class="card" style="margin-bottom:16px">
<div style="font-size:13px;font-weight:700;margin-bottom:10px">📊 Balances</div>
<div id="balanceBars"></div>
</div>
<div class="card" style="margin-bottom:16px">
<h4 style="margin-bottom:14px">Add Expense</h4>
<div class="upload-zone" style="margin-bottom:14px" id="tripDropZone">
<input type="file" id="tripFileInput" accept="image/*,.pdf">
<div style="font-size:24px;margin-bottom:4px">📸</div>
<div style="font-size:13px;font-weight:600;color:var(--text1)">Scan a receipt</div>
<div style="font-size:11px;color:var(--text2)">AI extracts vendor, amount, currency</div>
</div>
<div style="text-align:center;margin:10px 0;color:var(--text2);font-size:12px">— or add manually —</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
<div style="grid-column:1/-1"><label>Description *</label><input type="text" id="expDesc" placeholder="e.g. Dinner at O'Briens"></div>
<div><label>Amount *</label><input type="number" id="expAmount" placeholder="45.00" min="0" step="0.01"></div>
<div><label>Currency</label><select id="expCurrency"><option value="EUR">EUR</option><option value="USD">USD</option><option value="GBP">GBP</option><option value="INR">INR</option><option value="CAD">CAD</option><option value="MYR">MYR</option><option value="SGD">SGD</option><option value="AUD">AUD</option><option value="AED">AED</option><option value="JPY">JPY</option></select></div>
<div><label>Paid By *</label><select id="expPaidBy" style="width:100%;padding:10px;background:var(--bg);border:1.5px solid var(--border);border-radius:8px;color:#fff;font-size:14px;font-family:inherit"></select></div>
<div><label>Date</label><input type="date" id="expDate"></div>
<div style="grid-column:1/-1"><label>Split Among</label><div id="splitCheckboxes" style="display:flex;flex-wrap:wrap;gap:8px;margin-top:6px"></div></div>
<div style="grid-column:1/-1;text-align:right"><button class="btn btn-primary btn-sm" onclick="addExpense()">Add Expense</button></div>
</div>
</div>
<div style="margin-bottom:10px;font-size:13px;font-weight:700">Expenses</div>
<div id="expenseList"></div>
</div>
</div>

<script>
let currentTrip=null,tripData=null;

async function loadTrips(){
  try{const r=await fetch('/api/trips');
    if(!r.ok){document.getElementById('tripList').innerHTML='<div style="color:var(--red);text-align:center;padding:20px">API error: '+r.status+'</div>';return;}
    const d=await r.json();
    const el=document.getElementById('tripList');
    if(!d.trips||!d.trips.length){el.innerHTML='<div style="text-align:center;padding:40px;color:var(--text2)"><div style="font-size:48px;margin-bottom:12px">✈️</div><div style="font-size:16px;font-weight:700;color:var(--text1);margin-bottom:6px">No trips yet</div><div style="font-size:13px">Create your first trip to start splitting expenses</div></div>';return;}
    el.innerHTML=d.trips.map(t=>'<div class="trip-card" data-id="'+t.id+'"><div style="display:flex;justify-content:space-between;align-items:flex-start"><div><div style="font-size:15px;font-weight:700;color:var(--text1)">\u2708\ufe0f '+t.name+'</div><div style="font-size:12px;color:var(--text2);margin-top:4px">'+t.members.join(', ')+' \u00b7 '+t.currency+'</div></div><div style="text-align:right"><div style="font-size:16px;font-weight:700;color:var(--accent2)">'+t.currency+' '+t.total.toFixed(2)+'</div><div style="font-size:11px;color:var(--text2)">'+t.expense_count+' expenses</div></div></div></div>').join('');el.querySelectorAll('.trip-card').forEach(c=>c.onclick=()=>openTrip(c.dataset.id));
  }catch(e){document.getElementById('tripList').innerHTML='<div style="color:var(--red);text-align:center;padding:20px">Error: '+e.message+'</div>';}
}

function showNewTrip(){document.getElementById('newTripForm').classList.remove('hidden');}
function hideNewTrip(){document.getElementById('newTripForm').classList.add('hidden');}

async function createTrip(){
  const name=document.getElementById('tripName').value.trim();
  const members=document.getElementById('tripMembers').value.split(',').map(m=>m.trim()).filter(m=>m);
  const currency=document.getElementById('tripCurrency').value;
  if(!name||members.length<2){alert('Name + at least 2 members required');return;}
  const r=await fetch('/api/trips',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,members,currency})});
  const d=await r.json();if(d.success){hideNewTrip();document.getElementById('tripName').value='';document.getElementById('tripMembers').value='';loadTrips();openTrip(d.trip_id);}
}

function backToList(){document.getElementById('tripDetail').classList.add('hidden');document.getElementById('tripList').classList.remove('hidden');document.querySelector('[onclick="showNewTrip()"]').style.display='';loadTrips();}

async function openTrip(id){
  currentTrip=id;document.getElementById('tripList').classList.add('hidden');document.querySelector('[onclick="showNewTrip()"]').style.display='none';document.getElementById('tripDetail').classList.remove('hidden');
  try{
  const r=await fetch('/api/trips/'+id+'/expenses');
  tripData=await r.json();
  if(!tripData.trip||!tripData.members){document.getElementById('settlementsList').innerHTML='<div style="color:var(--red);font-size:13px">Failed to load: '+(tripData.error||'no data')+'</div>';return;}
  document.getElementById('detailName').textContent='✈️ '+(tripData.trip.name||'Trip')+' ('+(tripData.trip.currency||'EUR')+')';
  const total=(tripData.expenses||[]).reduce((s,e)=>s+(e.amount_base||0),0);
  document.getElementById('detailTotal').textContent=(tripData.trip.currency||'EUR')+' '+total.toFixed(2);
  document.getElementById('detailPerPerson').textContent=(tripData.trip.currency||'EUR')+' '+(total/Math.max((tripData.members||[]).length,1)).toFixed(2);
  // Settlements
  const sl=document.getElementById('settlementsList');
  const sa=document.getElementById('settleActions');
  const settledSet=new Set((tripData.settled_payments||[]).map(s=>s.from+'→'+s.to));
  const allSettled=!tripData.settlements.length || tripData.settlements.every(s=>settledSet.has(s.from+'→'+s.to));
  
  if(!tripData.settlements.length){
    sl.innerHTML='<div style="text-align:center;padding:16px"><div style="font-size:32px;margin-bottom:8px">🎉</div><div style="font-size:15px;font-weight:700;color:var(--accent2)">All settled!</div><div style="font-size:12px;color:var(--text2);margin-top:4px">No outstanding payments</div></div>';
    sa.innerHTML='';
  } else if(allSettled){
    sl.innerHTML='<div style="text-align:center;padding:16px"><div style="font-size:32px;margin-bottom:8px">✅</div><div style="font-size:15px;font-weight:700;color:var(--accent2)">Trip Settled!</div><div style="font-size:12px;color:var(--text2);margin-top:4px">All payments have been made</div></div>';
    sa.innerHTML='<button class="btn btn-ghost btn-sm" style="font-size:11px;padding:4px 10px" onclick="resetSettlements()">Reset</button>';
  } else {
    sl.innerHTML=tripData.settlements.map((s,idx)=>{
      const key=s.from+'→'+s.to;
      const isPaid=settledSet.has(key);
      return '<div class="settlement-arrow" style="'+(isPaid?'opacity:0.5;':'')+'"><span style="font-weight:700;color:var(--red)">'+s.from+'</span><span style="color:var(--text2);flex:1"> \u2192 pays <strong>'+tripData.trip.currency+' '+s.amount.toFixed(2)+'</strong> to </span><span style="font-weight:700;color:var(--accent2)">'+s.to+'</span>'+(isPaid?'<span style="color:var(--accent2);font-weight:700;margin-left:8px">\u2705 Paid</span><button class="btn btn-ghost btn-sm" style="padding:2px 8px;font-size:10px;margin-left:4px" onclick="unsettleIdx('+idx+')">Undo</button>':'<button class="btn btn-primary btn-sm" style="padding:4px 12px;font-size:11px;margin-left:8px;white-space:nowrap" onclick="settleIdx('+idx+')">Mark Paid</button>')+'</div>';
    }).join('');
    const unpaidCount=tripData.settlements.filter(s=>!settledSet.has(s.from+'→'+s.to)).length;
    sa.innerHTML=unpaidCount>1?'<button class="btn btn-primary btn-sm" style="font-size:11px;padding:4px 10px" onclick="settleAll()">Settle All</button>':'';
  }
  // Balances
  const bb=document.getElementById('balanceBars');const maxBal=Math.max(...Object.values(tripData.balances).map(Math.abs),1);
  bb.innerHTML=Object.entries(tripData.balances).map(([m,b])=>{const pct=Math.abs(b)/maxBal*100;const color=b>=0?'var(--accent)':'var(--red)';return '<div class="balance-bar"><div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px"><span style="font-weight:600">'+m+'</span><span style="color:'+color+';font-weight:700">'+(b>=0?'+':'')+b.toFixed(2)+'</span></div><div style="background:var(--bg);border-radius:4px;overflow:hidden"><div class="bar" style="width:'+pct+'%;background:'+color+';height:8px"></div></div></div>';}).join('');
  // Populate selects
  const paidBy=document.getElementById('expPaidBy');paidBy.innerHTML=tripData.members.map(m=>'<option>'+m+'</option>').join('');
  const sc=document.getElementById('splitCheckboxes');sc.innerHTML=tripData.members.map(m=>'<label style="display:flex;align-items:center;gap:4px;font-size:12px;cursor:pointer"><input type="checkbox" checked value="'+m+'"> '+m+'</label>').join('');
  document.getElementById('expCurrency').value=tripData.trip.currency;
  // Expenses
  const el=document.getElementById('expenseList');
  if(!tripData.expenses.length){el.innerHTML='<div style="text-align:center;padding:20px;color:var(--text2);font-size:13px">No expenses yet</div>';}
  else{el.innerHTML=tripData.expenses.map((e,ei)=>'<div class="card" style="padding:14px;margin-bottom:8px"><div style="display:flex;justify-content:space-between;align-items:flex-start"><div><div style="font-weight:600;font-size:14px">'+e.description+'</div><div style="font-size:12px;color:var(--text2);margin-top:2px">Paid by <strong>'+e.paid_by+'</strong>'+(e.date?' \u00b7 '+e.date:'')+(e.currency!==tripData.trip.currency?' \u00b7 '+e.currency+' '+e.amount.toFixed(2):'')+'</div></div><div style="text-align:right"><div style="font-weight:700;color:var(--accent2)">'+tripData.trip.currency+' '+e.amount_base.toFixed(2)+'</div><button class="btn btn-danger btn-sm" style="margin-top:6px;padding:4px 8px;font-size:10px" data-eidx="'+ei+'" onclick="deleteExpIdx('+ei+')">\u2715</button></div></div></div>').join('');}
  }catch(err){console.error('openTrip error:',err);document.getElementById('detailName').textContent='⚠️ Error';document.getElementById('settlementsList').innerHTML='<div style="color:var(--red);font-size:13px;padding:12px;background:rgba(255,0,0,0.1);border-radius:8px;word-break:break-all">Error: '+err.message+'<br><br>Try refreshing the page. If this persists, the app may still be deploying.</div>';}
}

async function addExpense(){
  const desc=document.getElementById('expDesc').value.trim(),amount=document.getElementById('expAmount').value,currency=document.getElementById('expCurrency').value,paid_by=document.getElementById('expPaidBy').value,date=document.getElementById('expDate').value;
  const split_among=Array.from(document.querySelectorAll('#splitCheckboxes input:checked')).map(c=>c.value);
  if(!desc||!amount||!paid_by){alert('Fill description, amount, paid by');return;}
  const r=await fetch('/api/trips/'+currentTrip+'/expenses',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({description:desc,amount:parseFloat(amount),currency,paid_by,split_among,date})});
  const d=await r.json();if(d.success){document.getElementById('expDesc').value='';document.getElementById('expAmount').value='';openTrip(currentTrip);}
}

async function deleteExpense(id){
  if(!confirm('Delete this expense?'))return;
  await fetch('/api/trips/'+currentTrip+'/expenses/'+id,{method:'DELETE'});openTrip(currentTrip);
}
function deleteExpIdx(idx){var e=tripData.expenses[idx];if(e)deleteExpense(e.id);}

async function settlePayment(from_m,to_m,amount){
  await fetch('/api/trips/'+currentTrip+'/settle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from:from_m,to:to_m,amount})});
  openTrip(currentTrip);
}
function settleIdx(idx){var s=tripData.settlements[idx];if(s)settlePayment(s.from,s.to,s.amount);}
async function unsettlePayment(from_m,to_m){
  await fetch('/api/trips/'+currentTrip+'/unsettle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from:from_m,to:to_m})});
  openTrip(currentTrip);
}
function unsettleIdx(idx){var s=tripData.settlements[idx];if(s)unsettlePayment(s.from,s.to);}
async function settleAll(){
  if(!confirm('Mark all payments as settled?'))return;
  await fetch('/api/trips/'+currentTrip+'/settle-all',{method:'POST'});
  openTrip(currentTrip);
}
async function resetSettlements(){
  if(!confirm('Reset all settlements? This will mark everything as unpaid again.'))return;
  await fetch('/api/trips/'+currentTrip+'/reset-settlements',{method:'POST'});
  openTrip(currentTrip);
}

// Receipt scan
document.getElementById('tripFileInput').addEventListener('change', async function(){
  if(!this.files.length||!currentTrip)return;
  const paid_by=document.getElementById('expPaidBy').value;
  const split_among=JSON.stringify(Array.from(document.querySelectorAll('#splitCheckboxes input:checked')).map(c=>c.value));
  document.getElementById('tripDropZone').innerHTML='<span class="spinner"></span> AI scanning receipt...';
  const fd=new FormData();fd.append('receipt',this.files[0]);fd.append('paid_by',paid_by);fd.append('split_among',split_among);
  try{const r=await fetch('/api/trips/'+currentTrip+'/scan',{method:'POST',body:fd});const d=await r.json();
    if(d.success){openTrip(currentTrip);}else{alert(d.error||'Scan failed');}
  }catch(e){alert('Scan error: '+e.message);}
  document.getElementById('tripDropZone').innerHTML='<input type="file" id="tripFileInput" accept="image/*,.pdf"><div style="font-size:24px;margin-bottom:4px">📸</div><div style="font-size:13px;font-weight:600;color:var(--text1)">Scan a receipt</div><div style="font-size:11px;color:var(--text2)">AI extracts vendor, amount, currency</div>';
  document.getElementById('tripFileInput').addEventListener('change',arguments.callee);
});

loadTrips();
</script></body></html>"""

ADMIN_HTML = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SplitSnap — Admin</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0B0F1A;--surface:#141926;--border:#2A3148;--text:#E8ECF4;--text2:#8B95B0;--accent:#10b981}
*{margin:0;padding:0;box-sizing:border-box}body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text)}
.hdr{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--border)}
.hdr h1{font-size:20px;font-weight:800;color:#fff}
.hdr a{color:var(--text2);text-decoration:none;font-size:13px;font-weight:600;margin-left:16px}
.content{max-width:900px;margin:0 auto;padding:20px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px}
.stat .label{font-size:12px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.5px}
.stat .value{font-size:28px;font-weight:800;color:#fff;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px;font-size:11px;font-weight:700;color:var(--text2);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
td{padding:10px;border-bottom:1px solid var(--border);color:var(--text)}
.badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:700;background:rgba(16,185,129,.15);color:var(--accent)}
</style></head><body>
<div class="hdr"><h1>✂️ SplitSnap Admin</h1><div><a href="/app">← Back to Tool</a><a href="/logout">Sign Out</a></div></div>
<div class="content">
<div class="stats">
<div class="stat"><div class="label">Total Users</div><div class="value">{{ total_users }}</div></div>
<div class="stat"><div class="label">Total Trips</div><div class="value">{{ total_trips }}</div></div>
<div class="stat"><div class="label">Total Expenses</div><div class="value">{{ total_expenses }}</div></div>
</div>
<h3 style="margin-bottom:12px;font-size:15px">Users</h3>
<div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden">
<table>
<thead><tr><th>Email</th><th>Name</th><th>Trips</th><th>Joined</th><th>Role</th></tr></thead>
<tbody>{% for u in users %}
<tr><td>{{ u.email }}</td><td>{{ u.name }}</td><td>{{ u.trip_count }}</td><td>{{ u.created_at.strftime('%Y-%m-%d') if u.created_at else '' }}</td><td>{% if u.is_superadmin %}<span class="badge">Admin</span>{% endif %}</td></tr>
{% endfor %}</tbody></table></div>
</div></body></html>"""

if __name__ == '__main__':
    app.run(debug=True, port=5007)
