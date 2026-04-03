from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, Response
from pymongo import MongoClient
from bson.objectid import ObjectId
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from datetime import datetime, timedelta
import random
import string
import os
import json
import csv
import io
import requests
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect
from google import genai
from google.genai import types

load_dotenv()

try:
    gemini_client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'))
except Exception as e:
    gemini_client = None
    print(f"[WARNING] Gemini API not configured: {e}")

app = Flask(__name__)
# Enable HTTPS proxy support for Vercel
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1, x_port=1)

# Security: Enable headers and CSRF protection
Talisman(app, content_security_policy=None)
CSRFProtect(app)

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["1000 per day", "300 per hour"],
    storage_uri="memory://"
)

from datetime import timedelta
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'eventapp-secret-key-2024')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365)

# Optimize PyMongo for Serverless/Vercel
import certifi
db_url = os.environ.get('DATABASE_URL', 'mongodb://localhost:27017/')
client = MongoClient(db_url, maxPoolSize=1, serverSelectionTimeoutMS=5000, tlsCAFile=certifi.where())
db = client.get_default_database('event_app') if 'event_app' not in db_url else client.get_database()

bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Jinja2 custom filter
@app.template_filter('from_json')
def from_json_filter(value):
    try:
        if isinstance(value, list):
            return value
        return json.loads(value or '[]')
    except Exception:
        return []

# ─────────────────────────────────────────────
#  AUTHORIZATION HELPERS
# ─────────────────────────────────────────────

def is_member(event_id, user_id):
    from bson.objectid import ObjectId
    try:
        member = db.event_members.find_one({"event_id": ObjectId(event_id), "user_id": ObjectId(user_id)})
        return member and member.get('status') == 'approved'
    except Exception:
        return False

def is_admin(event_id, user_id):
    from bson.objectid import ObjectId
    try:
        member = db.event_members.find_one({"event_id": ObjectId(event_id), "user_id": ObjectId(user_id)})
        return member and member.get('status') == 'approved' and member.get('role') == 'admin'
    except Exception:
        return False

# Google OAuth config — set these as environment variables before running
GOOGLE_CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    print("\n[WARNING] Google OAuth credentials are NOT set.")
    print("  Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET as environment variables.")
    print("  Google Sign-In will fail until these are configured.\n")

# ─────────────────────────────────────────────
#  MODELS (PyMongo Wrappers)
# ─────────────────────────────────────────────

class User(UserMixin):
    def __init__(self, user_dict):
        self.id = str(user_dict.get('_id'))
        self.name = user_dict.get('name', '')
        self.email = user_dict.get('email', '')
        self.password = user_dict.get('password', '')
        self.google_id = user_dict.get('google_id', '')
        self.avatar = user_dict.get('avatar', '')
        self.created_at = user_dict.get('created_at', datetime.utcnow())

    @staticmethod
    def get(user_id):
        try:
            user_dict = db.users.find_one({"_id": ObjectId(user_id)})
            if user_dict:
                return User(user_dict)
        except Exception:
            pass
        return None

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def generate_event_code():
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        if not db.events.find_one({"unique_code": code}):
            return code

def get_member(event_id, user_id):
    try:
        return db.event_members.find_one({
            "event_id": ObjectId(event_id) if isinstance(event_id, str) else event_id,
            "user_id": ObjectId(user_id) if isinstance(user_id, str) else user_id
        })
    except Exception:
        return None

def is_admin(event_id, user_id):
    m = get_member(event_id, user_id)
    return m and m.get('role') == 'admin'

def is_admin_or_manager(event_id, user_id):
    m = get_member(event_id, user_id)
    return m and m.get('role') in ('admin', 'manager')

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_notification_email(recipient_email, subject, body_html):
    # Get SMTP settings from environment
    SMTP_SERVER = os.environ.get('MAIL_SERVER')
    SMTP_PORT = os.environ.get('MAIL_PORT', 587)
    SMTP_USER = os.environ.get('MAIL_USERNAME')
    SMTP_PASS = os.environ.get('MAIL_PASSWORD')

    if not all([SMTP_SERVER, SMTP_USER, SMTP_PASS]):
        print(f"[WARNING] Email not sent to {recipient_email}. SMTP settings missing.")
        return False

    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_USER
        msg['To'] = recipient_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body_html, 'html'))

        with smtplib.SMTP(SMTP_SERVER, int(SMTP_PORT)) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"[ERROR] Failed to send email: {e}")
        return False

# Manual database initialization route
@app.route('/init-db')
def init_db():
    try:
        # Create necessary indexes for PyMongo collections
        db.users.create_index("email", unique=True)
        db.events.create_index("unique_code", unique=True)
        db.chat_history.create_index("updated_at", expireAfterSeconds=2592000) # 30 Days TTL
        return "<h1>Database Success!</h1><p>Indexes created. <a href='/login'>Go to Login</a></p>"
    except Exception as e:
        return f"<h1>Database Error</h1><p>{str(e)}</p>"


# ─────────────────────────────────────────────
#  AUTH ROUTES
# ─────────────────────────────────────────────

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        try:
            user_dict = db.users.find_one({"email": email})
            if user_dict and user_dict.get('password') and bcrypt.check_password_hash(user_dict['password'], password):
                session.permanent = True
                login_user(User(user_dict), remember=True)
                return redirect(url_for('dashboard'))
            flash('Invalid email or password.', 'error')
        except Exception as e:
            flash(f'Login Error: {str(e)}', 'error')
            print(f"Detailed Login Error: {e}")
    return render_template('auth.html', mode='login')

@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if not name or not email or not password:
            flash('All fields are required.', 'error')
            return render_template('auth.html', mode='register')
        if password != confirm:
            flash('Passwords do not match.', 'error')
            return render_template('auth.html', mode='register')
        try:
            if db.users.find_one({"email": email}):
                flash('Email already registered.', 'error')
                return render_template('auth.html', mode='register')
            
            # Generate OTP
            import random
            otp = str(random.randint(100000, 999999))
            hashed = bcrypt.generate_password_hash(password).decode('utf-8')
            
            # Clear existing OTPs for this email
            db.otp_verifications.delete_many({"email": email})
            
            # Store pending registration
            new_otp = {
                "email": email, 
                "name": name, 
                "password": hashed, 
                "otp_code": otp,
                "created_at": datetime.utcnow()
            }
            db.otp_verifications.insert_one(new_otp)
            
            # Send OTP Email
            sent = send_notification_email(email, "Verify your EventFlow Account", 
                                           render_template('emails/otp_email.html', name=name, otp=otp))
            if sent:
                session['pending_email'] = email
                flash('Verification code sent to your email!', 'success')
                return redirect(url_for('verify_otp'))
            else:
                flash('Failed to send verification email. Please check your SMTP settings.', 'error')
        except Exception as e:
            flash(f'Registration Error: {str(e)}', 'error')
            print(f"Detailed Registration Error: {e}")
            
    return render_template('auth.html', mode='register')

@app.route('/verify-otp', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def verify_otp():
    email = session.get('pending_email')
    if not email:
        return redirect(url_for('register'))
    
    if request.method == 'POST':
        otp = request.form.get('otp', '').strip()
        try:
            record = db.otp_verifications.find_one({"email": email, "otp_code": otp})
            
            if record:
                # Check for expiry (e.g., 10 minutes)
                if datetime.utcnow() - record.get('created_at', datetime.utcnow()) > timedelta(minutes=10):
                    flash('OTP has expired. Please resend.', 'error')
                    return render_template('verify_otp.html', email=email)
                
                # Create actual user
                new_user = {
                    "name": record['name'], 
                    "email": record['email'], 
                    "password": record['password'],
                    "created_at": datetime.utcnow()
                }
                res = db.users.insert_one(new_user)
                new_user['_id'] = res.inserted_id
                
                db.otp_verifications.delete_many({"email": email})
                
                session.permanent = True
                login_user(User(new_user), remember=True)
                session.pop('pending_email', None)
                flash('Account verified and created!', 'success')
                return redirect(url_for('setup_event'))
            else:
                flash('Invalid verification code.', 'error')
        except Exception as e:
            flash(f'Verification Error: {str(e)}', 'error')
            print(f"Detailed Verification Error: {e}")
            
    return render_template('verify_otp.html', email=email)

@app.route('/resend-otp')
@limiter.limit("3 per minute")
def resend_otp():
    email = session.get('pending_email')
    if not email:
        return redirect(url_for('register'))
    
    record = db.otp_verifications.find_one({"email": email}, sort=[("created_at", -1)])
    if not record:
        return redirect(url_for('register'))
    
    import random
    otp = str(random.randint(100000, 999999))
    db.otp_verifications.update_one(
        {"_id": record["_id"]},
        {"$set": {"otp_code": otp, "created_at": datetime.utcnow()}}
    )
    
    sent = send_notification_email(email, "New Verification Code - EventFlow", 
                                   render_template('emails/otp_email.html', name=record['name'], otp=otp))
    if sent:
        flash('New verification code sent!', 'success')
    else:
        flash('Failed to resend email.', 'error')
        
    return redirect(url_for('verify_otp'))

@app.route('/google-login')
def google_login():
    # Attempt to get redirect URI from env variable for manual override
    redirect_uri = os.environ.get('GOOGLE_REDIRECT_URI')
    if not redirect_uri:
        # Generate automatically, respecting proxy headers thanks to ProxyFix
        redirect_uri = url_for('google_callback', _external=True)
    
    # Debug log for Vercel console to help identify mismatches
    print(f"[DEBUG] Google Login Redirect URI: {redirect_uri}")
    
    google_auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        "?response_type=code"
        f"&client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        "&scope=openid%20email%20profile"
    )
    return redirect(google_auth_url)

@app.route('/google-callback')
def google_callback():
    code = request.args.get('code')
    if not code:
        flash('Google login failed.', 'error')
        return redirect(url_for('login'))
        
    # Must use the EXACT same redirect_uri as sent in /google-login
    redirect_uri = os.environ.get('GOOGLE_REDIRECT_URI')
    if not redirect_uri:
        redirect_uri = url_for('google_callback', _external=True)
        
    try:
        token_resp = requests.post('https://oauth2.googleapis.com/token', data={
            'code': code,
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
        })
        tokens = token_resp.json()
        access_token = tokens.get('access_token')
        userinfo_resp = requests.get('https://www.googleapis.com/oauth2/v3/userinfo',
                                     headers={'Authorization': f'Bearer {access_token}'})
        userinfo = userinfo_resp.json()
        google_id = userinfo.get('sub')
        email = userinfo.get('email', '').lower()
        name = userinfo.get('name', email)
        picture = userinfo.get('picture', '')
        
        user_dict = db.users.find_one({"email": email})
        if not user_dict:
            new_user = {
                "name": name, 
                "email": email, 
                "google_id": google_id, 
                "avatar": picture,
                "created_at": datetime.utcnow()
            }
            res = db.users.insert_one(new_user)
            new_user['_id'] = res.inserted_id
            user_dict = new_user
        else:
            update = {"google_id": google_id, "avatar": picture}
            if not user_dict.get('name'):
                update['name'] = name
            db.users.update_one({"_id": user_dict["_id"]}, {"$set": update})
            user_dict.update(update)
            
        session.permanent = True
        login_user(User(user_dict), remember=True)
        return redirect(url_for('dashboard'))
    except Exception as e:
        flash('Google login failed. Please try manual login.', 'error')
        return redirect(url_for('login'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ─────────────────────────────────────────────
#  USER PROFILE
# ─────────────────────────────────────────────

@app.route('/update-profile', methods=['POST'])
@login_required
def update_profile():
    name = request.form.get('name', '').strip()
    if name:
        db.users.update_one({"_id": ObjectId(current_user.id)}, {"$set": {"name": name}})
        flash('Profile updated successfully!', 'success')
    return redirect(request.referrer or url_for('dashboard'))

@app.route('/event/<event_id>/share-rsvp', methods=['POST'])
@login_required
def share_rsvp(event_id):
    if not is_member(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    email = data.get('email', '').strip()
    if not email:
        return jsonify({'error': 'Email is required'}), 400
        
    event = db.events.find_one({"_id": ObjectId(event_id)})
    
    body_html = f"""
    <h3>You're invited to {event['name']}!</h3>
    <p>Please let us know if you can make it by filling out your RSVP form here:</p>
    <a href="{url_for('public_rsvp', event_code=event['unique_code'], _external=True)}" style="display:inline-block; padding:10px 20px; background:#6366f1; color:#fff; text-decoration:none; border-radius:5px;">RSVP Now</a>
    """
    
    if send_notification_email(email, f"RSVP Invitation: {event['name']}", body_html):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to send email'}), 500


# ─────────────────────────────────────────────
#  EVENT SETUP (after first register)
# ─────────────────────────────────────────────

@app.route('/setup-event', methods=['GET', 'POST'])
@login_required
def setup_event():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'create':
            event_name = request.form.get('event_name', '').strip()
            if not event_name:
                flash('Event name is required.', 'error')
                return render_template('setup_event.html')
            code = generate_event_code()
            new_event = {
                "name": event_name, 
                "unique_code": code, 
                "description": "", 
                "theme_color": request.form.get('theme_color', '#6366f1'),
                "date": request.form.get('event_date', ''),
                "created_at": datetime.utcnow()
            }
            res = db.events.insert_one(new_event)
            event_id = res.inserted_id
            
            member = {
                "event_id": event_id, 
                "user_id": ObjectId(current_user.id),
                "role": 'admin', 
                "status": 'approved',
                "joined_at": datetime.utcnow()
            }
            db.event_members.insert_one(member)
            flash(f'Event created! Your event code is: {code}', 'success')
            return redirect(url_for('event_dashboard', event_id=str(event_id)))
            
        elif action == 'join':
            event_code = request.form.get('event_code', '').strip().upper()
            event = db.events.find_one({"unique_code": event_code})
            if not event:
                flash('Invalid event code.', 'error')
                return render_template('setup_event.html')
                
            existing = get_member(event["_id"], current_user.id)
            if existing:
                flash('You are already part of this event.', 'info')
                return redirect(url_for('event_dashboard', event_id=str(event["_id"])))
                
            member = {
                "event_id": event["_id"], 
                "user_id": ObjectId(current_user.id),
                "role": 'member', 
                "status": 'pending',
                "joined_at": datetime.utcnow()
            }
            db.event_members.insert_one(member)
            
            # Send Join Request Pending Email to Applicant
            send_notification_email(current_user.email, f"Join Request: {event['name']}", 
                                   render_template('emails/join_request_pending.html', 
                                                  user_name=current_user.name, 
                                                  event_name=event['name']))
            
            # Send Notification to Admin
            admin_member = db.event_members.find_one({"event_id": event["_id"], "role": 'admin'})
            if admin_member:
                admin_user = db.users.find_one({"_id": admin_member["user_id"]})
                if admin_user:
                    send_notification_email(admin_user['email'], f"New Join Request: {event['name']}",
                                           render_template('emails/admin_new_request.html',
                                                          event_name=event['name'],
                                                          applicant_name=current_user.name,
                                                          applicant_email=current_user.email,
                                                          dashboard_url=url_for('event_dashboard', event_id=str(event["_id"]), _external=True)))
                                                  
            flash('Request submitted! Waiting for admin approval.', 'success')
            return redirect(url_for('dashboard'))
    return render_template('setup_event.html')


# ─────────────────────────────────────────────
#  DASHBOARD (user's events)
# ─────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    memberships = list(db.event_members.find({"user_id": ObjectId(current_user.id), "status": 'approved'}))
    pending = list(db.event_members.find({"user_id": ObjectId(current_user.id), "status": 'pending'}))
    
    events = []
    for m in memberships:
        evt = db.events.find_one({"_id": m["event_id"]})
        if evt:
            evt['id'] = str(evt['_id'])
            events.append((evt, m["role"]))
            
    pending_events = []
    for m in pending:
        evt = db.events.find_one({"_id": m["event_id"]})
        if evt:
            evt['id'] = str(evt['_id'])
            pending_events.append((evt, m["role"]))
            
    return render_template('dashboard.html', events=events, pending_events=pending_events)


# ─────────────────────────────────────────────
#  EVENT DASHBOARD
# ─────────────────────────────────────────────

@app.route('/event/<event_id>')
@login_required
def event_dashboard(event_id):
    try:
        event = db.events.find_one({"_id": ObjectId(event_id)})
    except Exception:
        event = None
        
    if not event:
        flash('Event not found.', 'error')
        return redirect(url_for('dashboard'))
    event['id'] = str(event['_id'])
        
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
        
    members_docs = list(db.event_members.find({"event_id": ObjectId(event_id)}))
    members = []
    for m in members_docs:
        u = db.users.find_one({"_id": m["user_id"]})
        m['id'] = str(m['_id'])
        if u:
            u['id'] = str(u['_id'])
            m['user'] = u
            members.append(m)
            
    pending_approvals = [m for m in members if m.get('status') == 'pending']
    
    return render_template('event_dashboard.html', event=event, member=member,
                           members=members, pending_approvals=pending_approvals,
                            is_admin=is_admin(event_id, current_user.id),
                            is_admin_or_manager=is_admin_or_manager(event_id, current_user.id))


@app.route('/event/<event_id>/share-code', methods=['POST'])
@login_required
def share_event_code(event_id):
    try:
        event = db.events.find_one({"_id": ObjectId(event_id)})
    except Exception:
        event = None
    if not event:
        return jsonify({'error': 'Event not found'}), 404
        
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    recipient_email = data.get('email', '').strip().lower()
    
    if not recipient_email or '@' not in recipient_email:
        return jsonify({'error': 'Invalid email address'}), 400
        
    dashboard_url = url_for('dashboard', _external=True)
    
    # Send Sharing Email
    subject = f"Invitation to join '{event['name']}' on EventFlow"
    body_html = render_template('emails/share_event_code.html', 
                                sender_name=current_user.name,
                                event_name=event['name'],
                                joining_code=event['unique_code'],
                                dashboard_url=dashboard_url)
    
    sent = send_notification_email(recipient_email, subject, body_html)
    
    if sent:
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'Failed to send email. Please check SMTP settings.'}), 500

@app.route('/event/<event_id>/update-settings', methods=['POST'])
@login_required
def update_event_settings(event_id):
    if not is_admin(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    update_data = {}
    
    if 'theme_color' in data:
        update_data['theme_color'] = data['theme_color']
    if 'event_date' in data:
        update_data['date'] = data['event_date']
        
    if update_data:
        db.events.update_one({"_id": ObjectId(event_id)}, {"$set": update_data})
        
    return jsonify({'success': True})

# ─────────────────────────────────────────────
#  MEMBER MANAGEMENT (Admin only)
# ─────────────────────────────────────────────

@app.route('/event/<event_id>/approve/<member_id>', methods=['POST'])
@login_required
def approve_member(event_id, member_id):
    if not is_admin(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
        
    try:
        member = db.event_members.find_one({"_id": ObjectId(member_id)})
        event = db.events.find_one({"_id": ObjectId(event_id)})
        u = db.users.find_one({"_id": member["user_id"]})
    except Exception:
        return jsonify({'error': 'Not found'}), 404
        
    db.event_members.update_one({"_id": ObjectId(member_id)}, {"$set": {"status": "approved"}})
    
    # Send Join Request Approved Email
    if u and event:
        send_notification_email(u['email'], f"Join Request Approved: {event['name']}", 
                               render_template('emails/join_request_approved.html', 
                                              user_name=u['name'], 
                                              event_name=event['name'],
                                              dashboard_url=url_for('event_dashboard', event_id=str(event["_id"]), _external=True)))
                                          
    return jsonify({'success': True})

@app.route('/event/<event_id>/reject/<member_id>', methods=['POST'])
@login_required
def reject_member(event_id, member_id):
    if not is_admin(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
        
    try:
        member = db.event_members.find_one({"_id": ObjectId(member_id)})
        event = db.events.find_one({"_id": ObjectId(event_id)})
        u = db.users.find_one({"_id": member["user_id"]})
    except Exception:
        return jsonify({'error': 'Not found'}), 404
    
    # Send Rejection Email before deleting
    if u and event:
        send_notification_email(u['email'], f"Join Request Declined: {event['name']}",
                               render_template('emails/join_request_rejected.html',
                                              user_name=u['name'],
                                              event_name=event['name']))
                                          
    # Delete the record to allow new join requests in the future
    db.event_members.delete_one({"_id": ObjectId(member_id)})
                                          
    return jsonify({'success': True})

@app.route('/event/<event_id>/set-role/<member_id>', methods=['POST'])
@login_required
def set_role(event_id, member_id):
    if not is_admin(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    role = data.get('role')
    if role not in ('admin', 'manager', 'member'):
        return jsonify({'error': 'Invalid role'}), 400
        
    try:
        db.event_members.update_one({"_id": ObjectId(member_id)}, {"$set": {"role": role}})
    except Exception:
        return jsonify({'error': 'Not found'}), 404
        
    return jsonify({'success': True})


# ─────────────────────────────────────────────
#  GUEST MANAGEMENT AND EXPORT
# ─────────────────────────────────────────────

@app.route('/event/<event_id>/guests/export', methods=['GET'])
@login_required
def export_guests(event_id):
    if not is_member(event_id, current_user.id):
        return "Unauthorized", 403
        
    event = db.events.find_one({"_id": ObjectId(event_id)})
    guests_list = list(db.guests.find({"event_id": ObjectId(event_id)}).sort("created_at", 1))
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Guest Type', 'Family Members', 'Food Preference', 'RSVP Status', 'Added Date'])
    
    for g in guests_list:
        g_type = 'Family' if g.get('is_family') else 'Individual'
        fam_str = ""
        if g.get('is_family') and g.get('family_members'):
            try:
                f_list = json.loads(g['family_members'])
                if isinstance(f_list, list):
                    fam_str = ", ".join(f_list)
            except:
                pass
                
        writer.writerow([
            g.get('name', ''),
            g_type,
            fam_str,
            g.get('food_preference', ''),
            g.get('coming_status', 'pending').capitalize(),
            g.get('created_at', datetime.utcnow()).strftime('%Y-%m-%d %H:%M')
        ])
        
    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = f'attachment; filename=guests_{event["unique_code"]}.csv'
    return response

@app.route('/event/<event_id>/guests')
@login_required
def guests(event_id):
    try:
        event = db.events.find_one({"_id": ObjectId(event_id)})
    except Exception:
        event = None
    if not event:
        flash('Event not found.', 'error')
        return redirect(url_for('dashboard'))
    event['id'] = str(event['_id'])
        
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
        
    guest_list_cursor = db.guests.find({"event_id": ObjectId(event_id), "parent_id": None}).sort("created_at", -1)
    guest_list = []
    for g in guest_list_cursor:
        g['id'] = str(g['_id'])
        if 'created_at' not in g:
            g['created_at'] = datetime.utcnow()
        guest_list.append(g)
        
    total_guests = db.guests.count_documents({"event_id": ObjectId(event_id)})
    total_individuals = db.guests.count_documents({"event_id": ObjectId(event_id), "is_family": False})
    total_families = db.guests.count_documents({"event_id": ObjectId(event_id), "is_family": True, "parent_id": None})
    
    return render_template('guests.html', event=event, guests=guest_list,
                           total_individuals=total_individuals,
                           total_families=total_families,
                           total_guests=total_guests,
                           member=member,
                           is_admin=is_admin(event_id, current_user.id),
                           is_admin_or_manager=is_admin_or_manager(event_id, current_user.id))

@app.route('/event/<event_id>/guests/add', methods=['POST'])
@login_required
def add_guest(event_id):
    if not ObjectId.is_valid(event_id):
        return jsonify({'error': 'Invalid event ID'}), 400
        
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    
    event = db.events.find_one({"_id": ObjectId(event_id)})
    if not event:
        return jsonify({'error': 'Event not found'}), 404
        
    data = request.get_json()
    name = data.get('name', '').strip()
    is_family = data.get('is_family', False)
    family_members = data.get('family_members', [])
    
    if not name:
        return jsonify({'error': 'Name is required'}), 400
        
    # Check for duplicates (case-insensitive and substring checking)
    existing_guests = list(db.guests.find({"event_id": ObjectId(event_id)}))
    new_names_to_check = [name.lower()]
    if is_family and family_members:
        new_names_to_check.extend([m.strip().lower() for m in family_members if m.strip()])
        
    for g in existing_guests:
        g_name_lower = g.get('name', '').lower()
        for new_n in new_names_to_check:
            # Prevent "Ruthvij" from being added if "Ruthvij Rane" exists, and vice versa
            if len(new_n) >= 3 and len(g_name_lower) >= 3:
                if new_n in g_name_lower or g_name_lower in new_n:
                    return jsonify({'error': f'A guest matching "{new_n.title()}" is already registered as "{g.get("name")}".'}), 400
        
    food_preference = data.get('food_preference', 'Veg')
    primary_guest = {
        "event_id": ObjectId(event_id),
        "name": name,
        "is_family": is_family,
        "family_members": json.dumps(family_members),
        "added_by": ObjectId(current_user.id),
        "food_preference": food_preference,
        "coming_status": "coming",
        "travel_mode": "not_decided",
        "ticket_status": "not_booked",
        "parent_id": None,
        "created_at": datetime.utcnow()
    }
    
    result = db.guests.insert_one(primary_guest)
    primary_guest_id = result.inserted_id
    
    if is_family and family_members:
        member_docs = []
        for m_name in family_members:
            if m_name.strip():
                m_guest = {
                    "event_id": ObjectId(event_id),
                    "name": m_name.strip(),
                    "is_family": True,
                    "family_members": "[]",
                    "added_by": ObjectId(current_user.id),
                    "food_preference": food_preference,
                    "coming_status": "coming",
                    "travel_mode": "not_decided",
                    "ticket_status": "not_booked",
                    "parent_id": primary_guest_id,
                    "created_at": datetime.utcnow()
                }
                member_docs.append(m_guest)
        if member_docs:
            db.guests.insert_many(member_docs)
            
    return jsonify({
        'success': True, 
        'guest': {
            'id': str(primary_guest_id),
            'name': name,
            'is_family': is_family,
            'family_members': primary_guest['family_members'],
            'added_by': str(current_user.id),
            'food_preference': food_preference,
            'coming_status': "coming",
            'travel_mode': "not_decided",
            'ticket_status': "not_booked",
            'parent_id': None
        }
    })

@app.route('/event/<event_id>/guests/<guest_id>/update', methods=['POST'])
@login_required
def update_guest(event_id, guest_id):
    try:
        guest = db.guests.find_one({"_id": ObjectId(guest_id)})
    except Exception:
        return jsonify({'error': 'Guest not found'}), 404
        
    if not guest:
        return jsonify({'error': 'Guest not found'}), 404
        
    if not is_admin_or_manager(event_id, current_user.id) and str(guest.get('added_by')) != str(current_user.id):
        return jsonify({'error': 'Unauthorized. You can only edit guests you added personally.'}), 403
    
    data = request.get_json()
    update = {}
    
    for field in ['name', 'coming_status', 'travel_mode', 'ticket_status', 'food_preference']:
        if field in data:
            if field == 'name':
                update[field] = data[field].strip()
            else:
                update[field] = data[field]
                
    # Handle Family Syncing
    if guest.get('is_family') and 'family_members' in data and not guest.get('parent_id'):
        new_members = [m.strip() for m in data['family_members'] if m.strip()]
        update['family_members'] = json.dumps(new_members)
        
        # Sync children records
        existing_children_cursor = db.guests.find({"parent_id": ObjectId(guest_id)})
        existing_children = {c['name'].strip(): c for c in existing_children_cursor}
        
        current_child_names = set(new_members)
        
        for m_name in new_members:
            if m_name not in existing_children:
                db.guests.insert_one({
                    "event_id": ObjectId(event_id),
                    "name": m_name,
                    "is_family": True,
                    "family_members": "[]",
                    "added_by": ObjectId(current_user.id),
                    "parent_id": ObjectId(guest_id),
                    "coming_status": update.get('coming_status', guest.get('coming_status', '')),
                    "travel_mode": update.get('travel_mode', guest.get('travel_mode', '')),
                    "ticket_status": update.get('ticket_status', guest.get('ticket_status', '')),
                    "food_preference": update.get('food_preference', guest.get('food_preference', '')),
                    "created_at": datetime.utcnow()
                })
        
        for name, child in existing_children.items():
            if name not in current_child_names:
                db.guests.delete_one({"_id": child["_id"]})
                db.room_guests.delete_many({"guest_id": child["_id"]})

    if update:
        db.guests.update_one({"_id": ObjectId(guest_id)}, {"$set": update})
        guest.update(update)

    # If this is a parent, cascade certain updates to children
    if not guest.get('parent_id'):
        child_updates = {}
        for field in ['coming_status', 'travel_mode', 'ticket_status', 'food_preference']:
            if field in data:
                child_updates[field] = data[field]
                
        if child_updates:
            db.guests.update_many({"parent_id": ObjectId(guest_id)}, {"$set": child_updates})

    return jsonify({'success': True, 'guest': guest_to_dict(guest)})

@app.route('/event/<event_id>/guests/<guest_id>/delete', methods=['POST'])
@login_required
def delete_guest(event_id, guest_id):
    try:
        guest = db.guests.find_one({"_id": ObjectId(guest_id)})
    except Exception:
        return jsonify({'error': 'Guest not found'}), 404
        
    if not guest:
        return jsonify({'error': 'Guest not found'}), 404
        
    if not is_admin_or_manager(event_id, current_user.id) and str(guest.get('added_by')) != str(current_user.id):
        return jsonify({'error': 'Unauthorized. You can only delete guests you added personally.'}), 403
        
    children = list(db.guests.find({"parent_id": ObjectId(guest_id)}))
    child_ids = [c["_id"] for c in children]
    
    if child_ids:
        db.room_guests.delete_many({"guest_id": {"$in": child_ids}})
        db.guests.delete_many({"parent_id": ObjectId(guest_id)})

    db.room_guests.delete_many({"guest_id": ObjectId(guest_id)})
    db.guests.delete_one({"_id": ObjectId(guest_id)})
    return jsonify({'success': True})

@app.route('/event/<event_id>/guests/list')
@login_required
def get_guests(event_id):
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    guests = list(db.guests.find({"event_id": ObjectId(event_id)}))
    return jsonify([guest_to_dict(g) for g in guests])

def guest_to_dict(g):
    parent_name = None
    if g.get('parent_id'):
        parent = db.guests.find_one({"_id": g['parent_id']})
        if parent:
            parent_name = parent.get('name')
            
    # Handle created_at carefully as it might be a string or datetime depending on older records
    created_at = g.get('created_at')
    if isinstance(created_at, datetime):
        created_at_str = created_at.strftime('%Y-%m-%d')
    else:
        created_at_str = str(created_at)

    return {
        'id': str(g.get('_id')),
        'name': g.get('name'),
        'is_family': g.get('is_family'),
        'family_members': json.loads(g.get('family_members') or '[]'),
        'coming_status': g.get('coming_status'),
        'travel_mode': g.get('travel_mode'),
        'ticket_status': g.get('ticket_status'),
        'food_preference': g.get('food_preference'),
        'parent_id': str(g.get('parent_id')) if g.get('parent_id') else None,
        'parent_name': parent_name,
        'created_at': created_at_str
    }


# ─────────────────────────────────────────────
#  STAY MANAGEMENT
# ─────────────────────────────────────────────

@app.route('/event/<event_id>/stay')
@login_required
def stay(event_id):
    try:
        event = db.events.find_one({"_id": ObjectId(event_id)})
    except Exception:
        return redirect(url_for('dashboard'))
    if not event:
        flash('Event not found.', 'error')
        return redirect(url_for('dashboard'))
    event['id'] = str(event['_id'])
        
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
        
    accommodations = list(db.accommodations.find({"event_id": ObjectId(event_id)}))
    return render_template('stay.html', event=event, accommodations=accommodations,
                           member=member,
                           is_admin=is_admin(event_id, current_user.id),
                           is_admin_or_manager=is_admin_or_manager(event_id, current_user.id))

@app.route('/event/<event_id>/stay/add-place', methods=['POST'])
@login_required
def add_place(event_id):
    if not is_admin_or_manager(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized. Only admins and managers can add accommodations.'}), 403
    data = request.get_json()
    place = {
        "event_id": ObjectId(event_id),
        "place_name": data.get('place_name', '').strip(),
        "place_type": data.get('place_type', 'Hotel')
    }
    res = db.accommodations.insert_one(place)
    return jsonify({'success': True, 'id': str(res.inserted_id), 'name': place['place_name'], 'type': place['place_type']})

@app.route('/event/<event_id>/stay/add-room', methods=['POST'])
@login_required
def add_room(event_id):
    if not is_admin_or_manager(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized. Only admins and managers can add rooms.'}), 403
    data = request.get_json()
    room = {
        "accommodation_id": ObjectId(data.get('accommodation_id')),
        "room_number": data.get('room_number', '').strip()
    }
    res = db.rooms.insert_one(room)
    return jsonify({'success': True, 'id': str(res.inserted_id), 'room_number': room['room_number']})

@app.route('/event/<event_id>/stay/assign-guest', methods=['POST'])
@login_required
def assign_guest_to_room(event_id):
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    room_id = ObjectId(data.get('room_id'))
    guest_id = ObjectId(data.get('guest_id'))
    
    # Remove existing assignment for this guest
    db.room_guests.delete_many({"guest_id": guest_id})
    db.room_guests.insert_one({"room_id": room_id, "guest_id": guest_id})
    
    # Auto-assign family members if this is a family head
    guest = db.guests.find_one({"_id": guest_id})
    if guest and guest.get('is_family') and not guest.get('parent_id'):
        children = list(db.guests.find({"parent_id": guest_id}))
        if children:
            child_ids = [c["_id"] for c in children]
            db.room_guests.delete_many({"guest_id": {"$in": child_ids}})
            new_rgs = [{"room_id": room_id, "guest_id": cid} for cid in child_ids]
            db.room_guests.insert_many(new_rgs)
            
    return jsonify({'success': True})

@app.route('/event/<event_id>/stay/remove-guest', methods=['POST'])
@login_required
def remove_guest_from_room(event_id):
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    guest_id = ObjectId(data.get('guest_id'))
    room_id = ObjectId(data.get('room_id'))
    
    guest = db.guests.find_one({"_id": guest_id})
    if not guest:
        return jsonify({'error': 'Not found'}), 404
        
    # Permission check: admin/manager OR guest owner
    if not is_admin_or_manager(event_id, current_user.id) and str(guest.get('added_by')) != str(current_user.id):
        return jsonify({'error': 'Unauthorized. You can only remove guests you added personally.'}), 403
        
    db.room_guests.delete_many({"guest_id": guest_id, "room_id": room_id})
    return jsonify({'success': True})

@app.route('/event/<event_id>/stay/data')
@login_required
def stay_data(event_id):
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    
    accs = list(db.accommodations.find({"event_id": ObjectId(event_id)}))
    result = []
    
    room_ids = []
    for acc in accs:
        rooms_data = []
        rooms = list(db.rooms.find({"accommodation_id": acc["_id"]}))
        for room in rooms:
            room_ids.append(room["_id"])
            assigned = []
            rgs = list(db.room_guests.find({"room_id": room["_id"]}))
            for rg in rgs:
                g = db.guests.find_one({"_id": rg["guest_id"]})
                if g:
                    assigned.append({
                        'id': str(g['_id']), 
                        'name': g.get('name'), 
                        'is_family': g.get('is_family'),
                        'added_by': str(g.get('added_by'))
                    })
            rooms_data.append({'id': str(room['_id']), 'number': room.get('room_number'), 'guests': assigned})
        result.append({'id': str(acc['_id']), 'name': acc.get('place_name'), 'type': acc.get('place_type'), 'rooms': rooms_data})
        
    # Only show guests NOT already assigned to a room
    assigned_guest_ids = [rg["guest_id"] for rg in db.room_guests.find({"room_id": {"$in": room_ids}})]
    guests = list(db.guests.find({"event_id": ObjectId(event_id), "_id": {"$nin": assigned_guest_ids}}))
    
    guest_list = []
    for g in guests:
        parent_name = None
        if g.get('parent_id'):
            p = db.guests.find_one({"_id": g['parent_id']})
            parent_name = p.get('name') if p else None
        guest_list.append({
            'id': str(g['_id']), 
            'name': g.get('name'), 
            'is_family': g.get('is_family'), 
            'parent_name': parent_name
        })
        
    return jsonify({'accommodations': result, 'guests': guest_list})

@app.route('/event/<event_id>/stay/delete-place/<place_id>', methods=['POST'])
@login_required
def delete_place(event_id, place_id):
    if not is_admin_or_manager(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
        
    rooms = list(db.rooms.find({"accommodation_id": ObjectId(place_id)}))
    for room in rooms:
        db.room_guests.delete_many({"room_id": room["_id"]})
        db.rooms.delete_one({"_id": room["_id"]})
        
    db.accommodations.delete_one({"_id": ObjectId(place_id)})
    return jsonify({'success': True})

@app.route('/event/<event_id>/stay/delete-room/<room_id>', methods=['POST'])
@login_required
def delete_room(event_id, room_id):
    if not is_admin_or_manager(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
        
    db.room_guests.delete_many({"room_id": ObjectId(room_id)})
    db.rooms.delete_one({"_id": ObjectId(room_id)})
    return jsonify({'success': True})


# ─────────────────────────────────────────────
#  TRAVEL MANAGEMENT
# ─────────────────────────────────────────────

@app.route('/event/<event_id>/travel')
@login_required
def travel(event_id):
    try:
        event = db.events.find_one({"_id": ObjectId(event_id)})
    except Exception:
        event = None
    if not event:
        flash('Event not found.', 'error')
        return redirect(url_for('dashboard'))
    event['id'] = str(event['_id'])
        
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
        
    if not is_admin(event_id, current_user.id):
        flash('Only admins can manage travel.', 'error')
        return redirect(url_for('event_dashboard', event_id=event_id))
        
    guests_cursor = db.guests.find({"event_id": ObjectId(event_id)})
    guests = []
    for g in guests_cursor:
        g['id'] = str(g['_id'])
        if g.get('parent_id'):
            parent = db.guests.find_one({"_id": g.get('parent_id')})
            if parent:
                parent['id'] = str(parent['_id'])
                g['parent'] = parent
        guests.append(g)
        
    return render_template('travel.html', event=event, guests=guests,
                           member=member,
                           is_admin=True,
                           is_admin_or_manager=True)


# ─────────────────────────────────────────────
#  NOTIFICATIONS
# ─────────────────────────────────────────────

@app.route('/event/<event_id>/notifications')
@login_required
def notifications(event_id):
    try:
        event = db.events.find_one({"_id": ObjectId(event_id)})
    except Exception:
        event = None
    if not event:
        flash('Event not found.', 'error')
        return redirect(url_for('dashboard'))
    event['id'] = str(event['_id'])
        
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    
    # Get direct notifications or broadcasts (receiver_id is None)
    notifs_cursor = db.notifications.find({
        "event_id": ObjectId(event_id),
        "$or": [
            {"receiver_id": ObjectId(current_user.id)},
            {"receiver_id": None}
        ]
    }).sort("created_at", -1)
    
    notifs = []
    for n in notifs_cursor:
        sender = db.users.find_one({"_id": n["sender_id"]})
        if sender:
            n['sender_name'] = sender.get('name')
            n['sender_avatar'] = sender.get('avatar')
        n['id'] = str(n['_id'])
        notifs.append(n)
    
    return render_template('notifications.html', event=event, member=member,
                           notifications=notifs,
                           is_admin=is_admin(event_id, current_user.id),
                           is_admin_or_manager=is_admin_or_manager(event_id, current_user.id))

@app.route('/event/<event_id>/notifications/send', methods=['POST'])
@login_required
def send_notification(event_id):
    if not ObjectId.is_valid(event_id):
        return jsonify({'error': 'Invalid Event ID'}), 400
        
    member = get_member(event_id, current_user.id)
    if not member or member.get('status') != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    message = data.get('message', '').strip()
    receiver_id = data.get('receiver_id') # Can be 'all', 'admins', or specific ID
    
    if not message:
        return jsonify({'error': 'Message cannot be empty'}), 400

    sent_emails = 0
    event = db.events.find_one({"_id": ObjectId(event_id)})
    if not event:
        return jsonify({'error': 'Event not found'}), 404
    
    curr_user_dict = {"name": current_user.name, "email": current_user.email}
    
    if receiver_id == 'all':
        if member.get('role') != 'admin':
            return jsonify({'error': 'Unauthorized'}), 403
        
        event_members = list(db.event_members.find({"event_id": ObjectId(event_id), "status": 'approved'}))
            
        for em in event_members:
            if str(em["user_id"]) != str(current_user.id):
                u = db.users.find_one({"_id": em["user_id"]})
                if u:
                    notif = {
                        "event_id": ObjectId(event_id), 
                        "sender_id": ObjectId(current_user.id), 
                        "receiver_id": em["user_id"], 
                        "message": message,
                        "is_read": False,
                        "created_at": datetime.utcnow()
                    }
                    db.notifications.insert_one(notif)
                    email_sent = send_notification_email(u['email'], f"New Notification: {event['name']}", 
                                            render_template('emails/notification_email.html', 
                                                            user=u, event=event, message=message, sender=curr_user_dict))
                    if email_sent: sent_emails += 1
    
    elif receiver_id == 'admins':
        admins = list(db.event_members.find({"event_id": ObjectId(event_id), "role": 'admin', "status": 'approved'}))
        for adm in admins:
            if str(adm["user_id"]) != str(current_user.id):
                u = db.users.find_one({"_id": adm["user_id"]})
                if u:
                    notif = {
                        "event_id": ObjectId(event_id), 
                        "sender_id": ObjectId(current_user.id), 
                        "receiver_id": adm["user_id"], 
                        "message": message,
                        "is_read": False,
                        "created_at": datetime.utcnow()
                    }
                    db.notifications.insert_one(notif)
                    email_sent = send_notification_email(u['email'], f"Admin Alert: {event['name']}", 
                                            render_template('emails/notification_email.html', 
                                                            user=u, event=event, message=message, sender=curr_user_dict))
                    if email_sent: sent_emails += 1
    
    else:
        # Single recipient
        target_user = db.users.find_one({"_id": ObjectId(receiver_id)})
        if not target_user:
            return jsonify({'error': 'User not found'}), 404
            
        notif = {
            "event_id": ObjectId(event_id), 
            "sender_id": ObjectId(current_user.id), 
            "receiver_id": target_user["_id"], 
            "message": message,
            "is_read": False,
            "created_at": datetime.utcnow()
        }
        db.notifications.insert_one(notif)
        email_sent = send_notification_email(target_user['email'], f"New Message from {current_user.name}", 
                                render_template('emails/notification_email.html', 
                                                user=target_user, event=event, message=message, sender=curr_user_dict))
        if email_sent: sent_emails += 1

    return jsonify({'success': True, 'emails_sent': sent_emails})

@app.route('/api/event/<event_id>/ai-draft-invite', methods=['POST'])
@login_required
def ai_draft_invite(event_id):
    if not ObjectId.is_valid(event_id): return jsonify({"error": "Invalid ID"}), 400
    if not is_member(event_id, current_user.id): return jsonify({"error": "Unauthorized"}), 403
    
    event = db.events.find_one({"_id": ObjectId(event_id)})
    data = request.json
    recipient = data.get('recipient_name', 'Guest')
    vibe = data.get('vibe', 'Elegant & Formal')
    
    prompt = f"""
    You are a premium event invitation designer for the 'EventFlow' platform.
    Generate a BEAUTIFUL, RESPONSIVE, and PROFESSIONAL HTML email invitation.
    
    EVENT DETAILS:
    - Event Name: {event.get('name')}
    - Date: {event.get('date')}
    - Unique Joining Code: {event.get('unique_code')}
    - Inviter: {current_user.name}
    - Recipient: {recipient}
    - Requested Vibe: {vibe}
    
    CRITICAL DESIGN RULES:
    1. Output ONLY the raw HTML code (starting with <!DOCTYPE html>). No markdown, no triple backticks.
    2. Use a modern, dark-themed aesthetic (background: #0f172a) with vibrant gradients (#6366f1 to #a855f7).
    3. Include CSS in a <style> block. Use Google Font 'Inter' or sans-serif.
    4. The email MUST feature the joining code '{event.get('unique_code')}' prominently in a styled box.
    5. Include a call-to-action button that says 'Join Event'.
    6. Ensure the tone matches the requested vibe: {vibe}.
    7. Make sure it looks like a premium, state-of-the-art invitation.
    """
    
    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt,
        )
        # Clean up any potential markdown residue
        html = response.text.replace('```html', '').replace('```', '').strip()
        return jsonify({"html": html})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/event/<event_id>/ai-send-invitation', methods=['POST'])
@login_required
def ai_send_invitation(event_id):
    if not is_member(event_id, current_user.id): return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    email = data.get('email')
    html_body = data.get('html_body')
    
    if not email or not html_body:
        return jsonify({"error": "Missing email or content"}), 400
        
    event = db.events.find_one({"_id": ObjectId(event_id)})
    subject = f"You're invited to {event.get('name')}!"
    
    success = send_notification_email(email, subject, html_body)
    return jsonify({"success": success})

@app.route('/event/<event_id>/notifications/mark-read', methods=['POST'])
@login_required
def mark_notifications_read(event_id):
    if not ObjectId.is_valid(event_id):
        return jsonify({'success': False, 'error': 'Invalid event ID'})
        
    db.notifications.update_many(
        {"event_id": ObjectId(event_id), "receiver_id": ObjectId(current_user.id), "is_read": False},
        {"$set": {"is_read": True}}
    )
    return jsonify({'success': True})

@app.route('/event/<event_id>/guests/import_csv', methods=['POST'])
@login_required
def import_guests_csv(event_id):
    if not is_member(event_id, current_user.id):
        return jsonify({"error": "Unauthorized"}), 403
        
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
        
    file = request.files['file']
    if not file.filename.endswith('.csv'):
        return jsonify({"error": "Only CSV files are allowed"}), 400
        
    try:
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        reader = csv.DictReader(stream)
        guests_to_insert = []
        is_admin_flag = is_admin_or_manager(event_id, current_user.id)
        
        for row in reader:
            name = row.get('Name', '').strip()
            if not name: continue
                
            food_pref = row.get('Food Preference', 'Veg').strip()
            if not food_pref: food_pref = 'Veg'
            
            is_family = row.get('Is Family', 'no').strip().lower() in ['yes', 'true', '1']
            
            guests_to_insert.append({
                "event_id": ObjectId(event_id),
                "name": name,
                "is_family": is_family,
                "family_members": "[]",
                "food_preference": food_pref,
                "coming_status": "pending",
                "approval_status": "approved" if is_admin_flag else "pending",
                "added_by": current_user.id,
                "created_at": datetime.utcnow()
            })
            
        if guests_to_insert:
            db.guests.insert_many(guests_to_insert)
            
        return jsonify({"success": True, "count": len(guests_to_insert)})
    except Exception as e:
        print(f"CSV Import Error: {e}")
        return jsonify({"error": "Failed to parse CSV file format"}), 500

@app.route('/event/<event_id>/notifications/unread-count')
@login_required
def unread_count(event_id):
    if not ObjectId.is_valid(event_id):
        return jsonify({'count': 0})
        
    count = db.notifications.count_documents({
        "event_id": ObjectId(event_id), 
        "receiver_id": ObjectId(current_user.id), 
        "is_read": False
    })
    return jsonify({'count': count})


@app.route('/event/<event_id>/send-reminders', methods=['POST'])
@login_required
def send_reminders(event_id):
    if not is_admin(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
        
    event = db.events.find_one({"_id": ObjectId(event_id)})
    members = list(db.event_members.find({"event_id": ObjectId(event_id), "status": 'approved'}))
    
    sent_count = 0
    for mem in members:
        user = db.users.find_one({"_id": mem['user_id']})
        if not user or not user.get('email'): continue
        
        user_guests = list(db.guests.find({"event_id": ObjectId(event_id), "added_by": mem['user_id']}))
        if not user_guests: continue
        
        guest_text = "<br>".join([f"- {g.get('name')} (Status: {g.get('coming_status', 'pending')})" for g in user_guests])
        
        body_html = f"""
        <html><body>
        <h3>Event Reminder: {event.get('name')} is coming up!</h3>
        <p>Hello {user.get('name', 'there')},</p>
        <p>This is a reminder for the upcoming event. Please review and finalize the details for your guests:</p>
        <p>Your managed guests:</p>
        <p>{guest_text}</p>
        <br>
        <p>You can review room assignments and travel details on the <a href="{url_for('event_dashboard', event_id=event_id, _external=True)}">event dashboard</a>.</p>
        <p>Thanks!</p>
        </body></html>
        """
        
        if send_notification_email(user['email'], f"Reminder: {event.get('name')}", body_html):
            sent_count += 1
            
    return jsonify({'success': True, 'count': sent_count})

# ─────────────────────────────────────────────
#  ITINERARY MANAGEMENT
# ─────────────────────────────────────────────

@app.route('/event/<event_id>/itinerary')
@login_required
def itinerary(event_id):
    if not is_member(event_id, current_user.id):
        return redirect(url_for('dashboard'))
    event = db.events.find_one({"_id": ObjectId(event_id)})
    if event:
        event['id'] = str(event['_id'])
    member = db.event_members.find_one({"event_id": ObjectId(event_id), "user_id": ObjectId(current_user.id)})
    is_admin = member['role'] == 'admin'
    items = list(db.itinerary.find({"event_id": ObjectId(event_id)}).sort([("date", 1), ("time", 1)]))
    return render_template('itinerary.html', event=event, member=member, is_admin=is_admin, itinerary=items)

@app.route('/event/<event_id>/itinerary/add', methods=['POST'])
@login_required
def add_itinerary(event_id):
    if not is_member(event_id, current_user.id):
        return jsonify({"error": "Unauthorized"}), 403
    member = db.event_members.find_one({"event_id": ObjectId(event_id), "user_id": ObjectId(current_user.id)})
    if member['role'] != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
        
    data = {
        "event_id": ObjectId(event_id),
        "title": request.form.get('title', '').strip(),
        "date": request.form.get('date', '').strip(),
        "time": request.form.get('time', '').strip(),
        "location": request.form.get('location', '').strip(),
        "description": request.form.get('description', '').strip(),
        "created_by": ObjectId(current_user.id)
    }
    db.itinerary.insert_one(data)
    flash("Added itinerary item", "success")
    return redirect(url_for('itinerary', event_id=event_id))

@app.route('/event/<event_id>/itinerary/<item_id>/delete', methods=['POST'])
@login_required
def delete_itinerary(event_id, item_id):
    member = db.event_members.find_one({"event_id": ObjectId(event_id), "user_id": ObjectId(current_user.id)})
    if not member or member['role'] != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    db.itinerary.delete_one({"_id": ObjectId(item_id), "event_id": ObjectId(event_id)})
    return jsonify({"success": True})

# ─────────────────────────────────────────────
#  TASKS MANAGEMENT
# ─────────────────────────────────────────────

@app.route('/event/<event_id>/tasks')
@login_required
def tasks(event_id):
    if not is_member(event_id, current_user.id):
        return redirect(url_for('dashboard'))
    event = db.events.find_one({"_id": ObjectId(event_id)})
    if event:
        event['id'] = str(event['_id'])
    member = get_member(event_id, current_user.id)
    is_admin_user = member['role'] == 'admin'
    
    tasks_list = list(db.tasks.find({"event_id": ObjectId(event_id)}).sort("created_at", -1))
    
    completed = [t for t in tasks_list if t.get('completed')]
    pending = [t for t in tasks_list if not t.get('completed')]
    progress = (len(completed) / len(tasks_list) * 100) if tasks_list else 0
    
    return render_template('tasks.html', event=event, member=member, is_admin=is_admin_user, 
                           pending_tasks=pending, completed_tasks=completed, progress=progress)

@app.route('/event/<event_id>/tasks/add', methods=['POST'])
@login_required
def add_task(event_id):
    if not is_admin_or_manager(event_id, current_user.id):
        return jsonify({"error": "Unauthorized"}), 403
        
    title = request.form.get('title', '').strip()
    if not title:
        flash("Task title required", "error")
        return redirect(url_for('tasks', event_id=event_id))
        
    task_data = {
        "event_id": ObjectId(event_id),
        "title": title,
        "completed": False,
        "created_by": ObjectId(current_user.id),
        "created_at": datetime.utcnow()
    }
    db.tasks.insert_one(task_data)
    flash("Task added", "success")
    return redirect(url_for('tasks', event_id=event_id))

@app.route('/event/<event_id>/tasks/<task_id>/toggle', methods=['POST'])
@login_required
def toggle_task(event_id, task_id):
    if not is_member(event_id, current_user.id):
        return jsonify({"error": "Unauthorized"}), 403
        
    task = db.tasks.find_one({"_id": ObjectId(task_id)})
    if not task:
        return jsonify({"error": "Not found"}), 404
        
    db.tasks.update_one({"_id": ObjectId(task_id)}, {"$set": {"completed": not task.get('completed')}})
    return jsonify({"success": True})

@app.route('/event/<event_id>/tasks/<task_id>/delete', methods=['POST'])
@login_required
def delete_task(event_id, task_id):
    if not is_admin_or_manager(event_id, current_user.id):
        return jsonify({"error": "Unauthorized"}), 403
    
    db.tasks.delete_one({"_id": ObjectId(task_id), "event_id": ObjectId(event_id)})
    return jsonify({"success": True})

# ─────────────────────────────────────────────
#  EXPENSES MANAGEMENT
# ─────────────────────────────────────────────

@app.route('/event/<event_id>/expenses')
@login_required
def expenses(event_id):
    if not is_member(event_id, current_user.id):
        return redirect(url_for('dashboard'))
    event = db.events.find_one({"_id": ObjectId(event_id)})
    if event:
        event['id'] = str(event['_id'])
    member = db.event_members.find_one({"event_id": ObjectId(event_id), "user_id": ObjectId(current_user.id)})
    is_admin = member['role'] == 'admin'
    
    items = list(db.expenses.find({"event_id": ObjectId(event_id)}).sort("date", -1))
    total = sum(float(item.get('amount', 0)) for item in items)
    
    return render_template('expenses.html', event=event, member=member, is_admin=is_admin, expenses=items, total=total)

@app.route('/event/<event_id>/expenses/add', methods=['POST'])
@login_required
def add_expense(event_id):
    if not is_member(event_id, current_user.id):
        return jsonify({"error": "Unauthorized"}), 403
    member = db.event_members.find_one({"event_id": ObjectId(event_id), "user_id": ObjectId(current_user.id)})
    if member['role'] != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
        
    data = {
        "event_id": ObjectId(event_id),
        "title": request.form.get('title', '').strip(),
        "category": request.form.get('category', 'General'),
        "amount": float(request.form.get('amount', 0)),
        "date": request.form.get('date', '').strip() or datetime.utcnow().strftime('%Y-%m-%d'),
        "created_by": ObjectId(current_user.id)
    }
    db.expenses.insert_one(data)
    flash("Expense added", "success")
    return redirect(url_for('expenses', event_id=event_id))

@app.route('/event/<event_id>/expenses/<item_id>/delete', methods=['POST'])
@login_required
def delete_expense(event_id, item_id):
    member = db.event_members.find_one({"event_id": ObjectId(event_id), "user_id": ObjectId(current_user.id)})
    if not member or member['role'] != 'admin':
        return jsonify({"error": "Unauthorized"}), 403
    db.expenses.delete_one({"_id": ObjectId(item_id), "event_id": ObjectId(event_id)})
    return jsonify({"success": True})

# ─────────────────────────────────────────────
#  PUBLIC SELF-RSVP
# ─────────────────────────────────────────────

@app.route('/rsvp/<event_code>', methods=['GET', 'POST'])
def public_rsvp(event_code):
    event = db.events.find_one({"unique_code": event_code.upper()})
    if not event:
        return "Event not found", 404
        
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        food_preference = request.form.get('food_preference', 'Veg')
        coming_status = request.form.get('coming_status', 'yes')
        is_family = request.form.get('is_family') == 'true'
        
        family_members = []
        if is_family:
            fam_str = request.form.get('family_members', '')
            family_members = [m.strip() for m in fam_str.split(',') if m.strip()]
            
        guest = {
            "event_id": event["_id"],
            "name": name,
            "is_family": is_family,
            "family_members": json.dumps(family_members),
            "food_preference": food_preference,
            "coming_status": coming_status,
            "travel_mode": "not_decided",
            "ticket_status": "not_booked",
            "parent_id": None,
            "approval_status": "pending",
            "created_at": datetime.utcnow()
        }
        db.guests.insert_one(guest)
        flash("Your RSVP has been sent successfully!", "success")
        return redirect(url_for('public_rsvp', event_code=event_code))
        
    return render_template('rsvp.html', event=event)

@app.route('/event/<event_id>/guests/<guest_id>/approve', methods=['POST'])
@login_required
def approve_rsvp(event_id, guest_id):
    if not is_admin(event_id, current_user.id):
        return jsonify({"error": "Unauthorized"}), 403
    db.guests.update_one({"_id": ObjectId(guest_id)}, {"$set": {"approval_status": "approved"}})
    return jsonify({"success": True})

@app.route('/api/event/<event_id>/analytics')
@login_required
def event_analytics(event_id):
    if not is_member(event_id, current_user.id):
        return jsonify({"error": "Unauthorized"}), 403
    
    pipeline_expenses = [
        {"$match": {"event_id": ObjectId(event_id)}},
        {"$group": {"_id": "$category", "total": {"$sum": "$amount"}}}
    ]
    expense_data = list(db.expenses.aggregate(pipeline_expenses))
    
    pipeline_food = [
        {"$match": {"event_id": ObjectId(event_id), "coming_status": "yes"}},
        {"$group": {"_id": "$food_preference", "count": {"$sum": 1}}}
    ]
    food_data = list(db.guests.aggregate(pipeline_food))
    
    return jsonify({
        "budget": {
            "labels": [str(x['_id']) for x in expense_data],
            "data": [float(x['total']) for x in expense_data]
        },
        "food": {
            "labels": [str(x['_id']) for x in food_data],
            "data": [int(x['count']) for x in food_data]
        }
    })

# ─────────────────────────────────────────────
#  AI CHATBOT (RootBot) - TOOL DEFINITIONS
# ─────────────────────────────────────────────

def get_guest_summary(event_id: str):
    """Returns guest RSVP counts, food preferences, and a list of all attending guests."""
    if not ObjectId.is_valid(event_id): return {"error": "Invalid Event ID"}
    guests = list(db.guests.find({"event_id": ObjectId(event_id)}))
    total = len(guests)
    coming = len([g for g in guests if g.get('coming_status') == 'coming'])
    pending = len([g for g in guests if g.get('coming_status') == 'pending'])
    veg = len([g for g in guests if g.get('food_preference') == 'Veg'])
    non_veg = len([g for g in guests if g.get('food_preference') == 'Non-veg'])
    names = [g.get('name') for g in guests]
    return {
        "status_summary": {"total": total, "attending": coming, "pending": pending},
        "food_split": {"veg": veg, "non_veg": non_veg},
        "all_guest_names": names
    }

def get_expense_summary(event_id: str):
    """Returns total spent and a breakdown of costs per category."""
    if not ObjectId.is_valid(event_id): return {"error": "Invalid Event ID"}
    pipeline = [
        {"$match": {"event_id": ObjectId(event_id)}},
        {"$group": {"_id": "$category", "total": {"$sum": "$amount"}}}
    ]
    data = list(db.expenses.aggregate(pipeline))
    total_spent = sum(item['total'] for item in data)
    breakdown = {item['_id']: item['total'] for item in data}
    return {"total_spent": round(total_spent, 2), "breakdown": breakdown}

def get_event_itinerary(event_id: str):
    """Returns the full schedule of events for this specific event."""
    if not ObjectId.is_valid(event_id): return {"error": "Invalid Event ID"}
    items = list(db.itinerary.find({"event_id": ObjectId(event_id)}).sort("date", 1))
    return [{"time": i.get('time'), "date": i.get('date'), "title": i.get('title'), "location": i.get('location')} for i in items]

def get_tasks_progress(event_id: str):
    """Returns count of completed versus pending tasks."""
    if not ObjectId.is_valid(event_id): return {"error": "Invalid Event ID"}
    total = db.tasks.count_documents({"event_id": ObjectId(event_id)})
    done = db.tasks.count_documents({"event_id": ObjectId(event_id), "status": "completed"})
    return {"total_tasks": total, "completed": done, "pending": total - done}

# ─────────────────────────────────────────────
#  AI CHATBOT (RootBot) - ROUTES
# ─────────────────────────────────────────────

@app.route('/api/chat/history', methods=['GET'])
@login_required
def get_chat_history():
    history_doc = db.chat_history.find_one({"user_id": ObjectId(current_user.id)})
    messages = history_doc.get("messages", []) if history_doc else []
    # Send all messages excluding the instruction
    return jsonify({'messages': messages})

@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.get_json()
    user_message = data.get('message', '').strip()
    event_id = data.get('event_id')
    
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400
        
    try:
        if not gemini_client:
            return jsonify({'error': 'Gemini API not configured properly.'}), 500
            
        history_doc = db.chat_history.find_one({"user_id": ObjectId(current_user.id)})
        messages = history_doc.get("messages", []) if history_doc else []
        
        # Prepare context for Gemini
        event_context = ""
        if event_id and ObjectId.is_valid(event_id):
            event = db.events.find_one({"_id": ObjectId(event_id)})
            if event:
                event_context = f"\nCURRENT CONTEXT: You are assisting with the event '{event.get('name')}'. The Event ID is '{event_id}'."

        system_instruction = (
            "You are RootBot, the powerful AI Data Agent for EventFlow. "
            "You have access to live database tools to answer questions about guests, expenses, tasks, and the itinerary."
            f"{event_context}\n"
            "**CORE CAPABILITIES:** "
            "- Use 'get_guest_summary' for RSVP and food preferences. "
            "- Use 'get_expense_summary' for budget and category spending. "
            "- Use 'get_event_itinerary' for the schedule. "
            "- Use 'get_tasks_progress' for the checklist status. "
            "**STRICT RULES:** "
            "1. ONLY answer questions based on the data retrieved from tools or the predefined EventFlow features. "
            "2. If an 'event_id' is provided in your context, always use it as the argument for your tools. "
            "3. If NO event is in context, ask the user to navigate to an event page so you can help with specific data. "
            "4. Never hallucinate features. "
            "Keep answers professional, helpful, and concise."
        )

        # Map MongoDB history to Gemini Content format
        contents = []
        for msg in messages[-10:]: # Last 10 messages for context
            role = 'user' if msg['role'] == 'user' else 'model'
            contents.append(types.Content(role=role, parts=[types.Part(text=msg['text'])]))
        
        # Append the current message
        contents.append(types.Content(role='user', parts=[types.Part(text=user_message)]))

        # Tool mapping
        tool_map = {
            "get_guest_summary": get_guest_summary,
            "get_expense_summary": get_expense_summary,
            "get_event_itinerary": get_event_itinerary,
            "get_tasks_progress": get_tasks_progress
        }

        # Generate content with tools
        response = gemini_client.models.generate_content(
            model='gemini-2.0-flash',
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[get_guest_summary, get_expense_summary, get_event_itinerary, get_tasks_progress]
            )
        )

        # Handle Function Calling Loop
        # For simplicity, we handle one level of tool calling which is usually enough for these queries
        if response.candidates[0].content.parts[0].function_call:
            call = response.candidates[0].content.parts[0].function_call
            if call.name in tool_map:
                # Execute the local function
                tool_result = tool_map[call.name](**call.args)
                
                # Send result back to Gemini
                contents.append(response.candidates[0].content) # Add the model's call
                contents.append(types.Content(
                    role='tool',
                    parts=[types.Part(function_response=types.FunctionResponse(
                        name=call.name,
                        response={"result": tool_result}
                    ))]
                ))
                
                # Final response
                response = gemini_client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=contents,
                    config=types.GenerateContentConfig(system_instruction=system_instruction)
                )

        text_response = response.text
        
        # Save to DB
        new_messages = [
            {"role": "user", "text": user_message},
            {"role": "bot", "text": text_response}
        ]
        
        db.chat_history.update_one(
            {"user_id": ObjectId(current_user.id)},
            {
                "$push": {"messages": {"$each": new_messages}},
                "$set": {"updated_at": datetime.utcnow()}
            },
            upsert=True
        )
        
        return jsonify({'reply': text_response})

    except Exception as e:
        print(f"RootBot Logic Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'I encountered an error while processing your request.'}), 500

if __name__ == '__main__':
    print('\n  [OK]  EventFlow is running at http://127.0.0.1:5000\n')
    app.run(debug=True, port=5000)
