from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from pymongo import MongoClient
from bson.objectid import ObjectId
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from datetime import datetime, timedelta
import random
import string
import os
import json
import requests
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect

load_dotenv()

app = Flask(__name__)
# Enable HTTPS proxy support for Vercel
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1, x_port=1)

# Security: Enable headers and CSRF protection
Talisman(app, content_security_policy=None)
CSRFProtect(app)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'eventapp-secret-key-2024')

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
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        try:
            user_dict = db.users.find_one({"email": email})
            if user_dict and user_dict.get('password') and bcrypt.check_password_hash(user_dict['password'], password):
                login_user(User(user_dict))
                return redirect(url_for('dashboard'))
            flash('Invalid email or password.', 'error')
        except Exception as e:
            flash(f'Login Error: {str(e)}', 'error')
            print(f"Detailed Login Error: {e}")
    return render_template('auth.html', mode='login')

@app.route('/register', methods=['GET', 'POST'])
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
                
                login_user(User(new_user))
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
            
        login_user(User(user_dict))
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
#  GUEST MANAGEMENT
# ─────────────────────────────────────────────

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


if __name__ == '__main__':
    print('\n  [OK]  EventFlow is running at http://127.0.0.1:5000\n')
    app.run(debug=True, port=5000)
