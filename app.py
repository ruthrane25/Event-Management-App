from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import joinedload
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
# We set content_security_policy=None initially to avoid breaking existing styles/scripts
# But HSTS, X-Frame-Options, etc. will be enabled.
Talisman(app, content_security_policy=None)
CSRFProtect(app)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'eventapp-secret-key-2024')
db_url = os.environ.get('DATABASE_URL', 'sqlite:///event_app.db')
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PREFERRED_URL_SCHEME'] = 'https' # Force HTTPS in generated links

# Optimize SQLAlchemy for Serverless/Vercel (Cold Starts)
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_size": 1,
    "max_overflow": 0,
    "pool_recycle": 3600,
    "pool_pre_ping": True,
}

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Note: Database tables are initialized via /init-db route if needed.
# Removed global db.create_all() to speed up Vercel cold starts.

# Jinja2 custom filter
@app.template_filter('from_json')
def from_json_filter(value):
    try:
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
#  MODELS
# ─────────────────────────────────────────────

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(256))
    google_id = db.Column(db.String(100))
    avatar = db.Column(db.String(300), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    unique_code = db.Column(db.String(10), unique=True, nullable=False)
    description = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class EventMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    role = db.Column(db.String(20), default='member')  # admin, manager, member
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    event = db.relationship('Event', backref='members')
    user = db.relationship('User', backref='event_memberships')

class Guest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    is_family = db.Column(db.Boolean, default=False)
    family_members = db.Column(db.Text, default='[]')  # JSON list of names
    coming_status = db.Column(db.String(10), default='')   # yes / no / ''
    travel_mode = db.Column(db.String(10), default='')  # Train / Car / ''
    ticket_status = db.Column(db.String(10), default='')  # done / pending / ''
    food_preference = db.Column(db.String(10), default='Veg') # Veg / Non-veg
    added_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    parent_id = db.Column(db.Integer, db.ForeignKey('guest.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    event = db.relationship('Event', backref='guests')
    parent = db.relationship('Guest', remote_side=[id], backref=db.backref('children', cascade="all, delete-orphan"))

class Accommodation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    place_name = db.Column(db.String(200), nullable=False)  # Hotel or Villa
    place_type = db.Column(db.String(20), default='Hotel')  # Hotel / Villa
    event = db.relationship('Event', backref='accommodations')

class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    accommodation_id = db.Column(db.Integer, db.ForeignKey('accommodation.id'), nullable=False)
    room_number = db.Column(db.String(50), nullable=False)
    accommodation = db.relationship('Accommodation', backref='rooms')

class RoomGuest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=False)
    guest_id = db.Column(db.Integer, db.ForeignKey('guest.id'), nullable=False)
    room = db.relationship('Room', backref='room_guests')
    guest = db.relationship('Guest', backref='room_assignments')

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('user.id')) # NULL for broadcast
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    event = db.relationship('Event', backref='notifications')
    sender = db.relationship('User', foreign_keys=[sender_id], backref='sent_notifications')
    receiver = db.relationship('User', foreign_keys=[receiver_id], backref='received_notifications')

class OTPVerification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    password = db.Column(db.String(200), nullable=False)
    otp_code = db.Column(db.String(6), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def generate_event_code():
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        if not Event.query.filter_by(unique_code=code).first():
            return code

def get_member(event_id, user_id):
    return EventMember.query.filter_by(event_id=event_id, user_id=user_id).first()

def is_admin(event_id, user_id):
    m = get_member(event_id, user_id)
    return m and m.role == 'admin'

def is_admin_or_manager(event_id, user_id):
    m = get_member(event_id, user_id)
    return m and m.role in ('admin', 'manager')

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
        with app.app_context():
            db.create_all()
        return "<h1>Database Success!</h1><p>Tables have been created. <a href='/login'>Go to Login</a></p>"
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
            user = User.query.filter_by(email=email).first()
            if user and user.password and bcrypt.check_password_hash(user.password, password):
                login_user(user)
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
            if User.query.filter_by(email=email).first():
                flash('Email already registered.', 'error')
                return render_template('auth.html', mode='register')
            
            # Generate OTP
            import random
            otp = str(random.randint(100000, 999999))
            hashed = bcrypt.generate_password_hash(password).decode('utf-8')
            
            # Clear existing OTPs for this email
            OTPVerification.query.filter_by(email=email).delete()
            
            # Store pending registration
            new_otp = OTPVerification(email=email, name=name, password=hashed, otp_code=otp)
            db.session.add(new_otp)
            db.session.commit()
            
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
            record = OTPVerification.query.filter_by(email=email, otp_code=otp).first()
            
            if record:
                # Check for expiry (e.g., 10 minutes)
                if datetime.utcnow() - record.created_at > timedelta(minutes=10):
                    flash('OTP has expired. Please resend.', 'error')
                    return render_template('verify_otp.html', email=email)
                
                # Create actual user
                user = User(name=record.name, email=record.email, password=record.password)
                db.session.add(user)
                db.session.delete(record)
                db.session.commit()
                
                login_user(user)
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
    
    record = OTPVerification.query.filter_by(email=email).order_by(OTPVerification.created_at.desc()).first()
    if not record:
        return redirect(url_for('register'))
    
    import random
    otp = str(random.randint(100000, 999999))
    record.otp_code = otp
    record.created_at = datetime.utcnow()
    db.session.commit()
    
    sent = send_notification_email(email, "New Verification Code - EventFlow", 
                                   render_template('emails/otp_email.html', name=record.name, otp=otp))
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
        user = User.query.filter_by(email=email).first()
        if not user:
            user = User(name=name, email=email, google_id=google_id, avatar=picture)
            db.session.add(user)
            db.session.commit()
        else:
            user.google_id = google_id
            user.avatar = picture
            if not user.name:
                user.name = name
            db.session.commit()
        login_user(user)
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
            event = Event(name=event_name, unique_code=code)
            db.session.add(event)
            db.session.flush()
            member = EventMember(event_id=event.id, user_id=current_user.id,
                                  role='admin', status='approved')
            db.session.add(member)
            db.session.commit()
            flash(f'Event created! Your event code is: {code}', 'success')
            return redirect(url_for('event_dashboard', event_id=event.id))
        elif action == 'join':
            event_code = request.form.get('event_code', '').strip().upper()
            event = Event.query.filter_by(unique_code=event_code).first()
            if not event:
                flash('Invalid event code.', 'error')
                return render_template('setup_event.html')
            existing = get_member(event.id, current_user.id)
            if existing:
                flash('You are already part of this event.', 'info')
                return redirect(url_for('event_dashboard', event_id=event.id))
            member = EventMember(event_id=event.id, user_id=current_user.id,
                                  role='member', status='pending')
            db.session.add(member)
            db.session.commit()
            
            # Send Join Request Pending Email to Applicant
            send_notification_email(current_user.email, f"Join Request: {event.name}", 
                                   render_template('emails/join_request_pending.html', 
                                                  user_name=current_user.name, 
                                                  event_name=event.name))
            
            # Send Notification to Admin
            admin_member = EventMember.query.filter_by(event_id=event.id, role='admin').first()
            if admin_member and admin_member.user:
                send_notification_email(admin_member.user.email, f"New Join Request: {event.name}",
                                       render_template('emails/admin_new_request.html',
                                                      event_name=event.name,
                                                      applicant_name=current_user.name,
                                                      applicant_email=current_user.email,
                                                      dashboard_url=url_for('event_dashboard', event_id=event.id, _external=True)))
                                                  
            flash('Request submitted! Waiting for admin approval.', 'success')
            return redirect(url_for('dashboard'))
    return render_template('setup_event.html')


# ─────────────────────────────────────────────
#  DASHBOARD (user's events)
# ─────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    # Eager load event for each membership to avoid N+1 query problem
    memberships = EventMember.query.options(joinedload(EventMember.event)) \
        .filter_by(user_id=current_user.id, status='approved').all()
    pending = EventMember.query.options(joinedload(EventMember.event)) \
        .filter_by(user_id=current_user.id, status='pending').all()
    events = [(m.event, m.role) for m in memberships]
    pending_events = [(m.event, m.role) for m in pending]
    return render_template('dashboard.html', events=events, pending_events=pending_events)


# ─────────────────────────────────────────────
#  EVENT DASHBOARD
# ─────────────────────────────────────────────

@app.route('/event/<int:event_id>')
@login_required
def event_dashboard(event_id):
    event = Event.query.get_or_404(event_id)
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    members = EventMember.query.filter_by(event_id=event_id).all()
    pending_approvals = EventMember.query.filter_by(event_id=event_id, status='pending').all()
    return render_template('event_dashboard.html', event=event, member=member,
                           members=members, pending_approvals=pending_approvals,
                            is_admin=is_admin(event_id, current_user.id),
                            is_admin_or_manager=is_admin_or_manager(event_id, current_user.id))


@app.route('/event/<int:event_id>/share-code', methods=['POST'])
@login_required
def share_event_code(event_id):
    event = Event.query.get_or_404(event_id)
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    recipient_email = data.get('email', '').strip().lower()
    
    if not recipient_email or '@' not in recipient_email:
        return jsonify({'error': 'Invalid email address'}), 400
        
    dashboard_url = url_for('dashboard', _external=True)
    
    # Send Sharing Email
    subject = f"Invitation to join '{event.name}' on EventFlow"
    body_html = render_template('emails/share_event_code.html', 
                                sender_name=current_user.name,
                                event_name=event.name,
                                joining_code=event.unique_code,
                                dashboard_url=dashboard_url)
    
    sent = send_notification_email(recipient_email, subject, body_html)
    
    if sent:
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'Failed to send email. Please check SMTP settings.'}), 500


# ─────────────────────────────────────────────
#  MEMBER MANAGEMENT (Admin only)
# ─────────────────────────────────────────────

@app.route('/event/<int:event_id>/approve/<int:member_id>', methods=['POST'])
@login_required
def approve_member(event_id, member_id):
    if not is_admin(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
    member = EventMember.query.get_or_404(member_id)
    member.status = 'approved'
    db.session.commit()
    
    # Send Join Request Approved Email
    send_notification_email(member.user.email, f"Join Request Approved: {member.event.name}", 
                           render_template('emails/join_request_approved.html', 
                                          user_name=member.user.name, 
                                          event_name=member.event.name,
                                          dashboard_url=url_for('event_dashboard', event_id=member.event_id, _external=True)))
                                          
    return jsonify({'success': True})

@app.route('/event/<int:event_id>/reject/<int:member_id>', methods=['POST'])
@login_required
def reject_member(event_id, member_id):
    if not is_admin(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
    member = EventMember.query.get_or_404(member_id)
    
    # Send Rejection Email before deleting
    send_notification_email(member.user.email, f"Join Request Declined: {member.event.name}",
                           render_template('emails/join_request_rejected.html',
                                          user_name=member.user.name,
                                          event_name=member.event.name))
                                          
    # Delete the record to allow new join requests in the future
    db.session.delete(member)
    db.session.commit()
                                          
    return jsonify({'success': True})

@app.route('/event/<int:event_id>/set-role/<int:member_id>', methods=['POST'])
@login_required
def set_role(event_id, member_id):
    if not is_admin(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    role = data.get('role')
    if role not in ('admin', 'manager', 'member'):
        return jsonify({'error': 'Invalid role'}), 400
    member = EventMember.query.get_or_404(member_id)
    member.role = role
    db.session.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────────
#  GUEST MANAGEMENT
# ─────────────────────────────────────────────

@app.route('/event/<int:event_id>/guests')
@login_required
def guests(event_id):
    event = Event.query.get_or_404(event_id)
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    # Only show primary guests (those without a parent) in the main list
    guest_list = Guest.query.filter_by(event_id=event_id, parent_id=None).all()
    # Stats should count EVERYONE
    total_guests = db.session.query(db.func.count(Guest.id)).filter(Guest.event_id == event_id).scalar() or 0
    total_individuals = Guest.query.filter_by(event_id=event_id, is_family=False).count()
    # Families count is still the number of specific family-head entries
    total_families = Guest.query.filter_by(event_id=event_id, is_family=True, parent_id=None).count()
    
    return render_template('guests.html', event=event, guests=guest_list,
                           total_individuals=total_individuals,
                           total_families=total_families,
                           total_guests=total_guests,
                           member=member,
                           is_admin=is_admin(event_id, current_user.id),
                           is_admin_or_manager=is_admin_or_manager(event_id, current_user.id))

@app.route('/event/<int:event_id>/guests/add', methods=['POST'])
@login_required
def add_guest(event_id):
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    
    event = Event.query.get_or_404(event_id)
    data = request.get_json()
    name = data.get('name', '').strip()
    is_family = data.get('is_family', False)
    family_members = data.get('family_members', [])
    
    if not name:
        return jsonify({'error': 'Name is required'}), 400
        
    # Create the primary guest record
    food_preference = data.get('food_preference', 'Veg')
    primary_guest = Guest(event_id=event_id, name=name, is_family=is_family,
                         family_members=json.dumps(family_members), added_by=current_user.id,
                         food_preference=food_preference)
    db.session.add(primary_guest)
    db.session.flush() # Get ID for children
    
    # If family, create individual records for each member
    if is_family and family_members:
        for m_name in family_members:
            if m_name.strip():
                m_guest = Guest(event_id=event_id, name=m_name.strip(), is_family=True,
                               added_by=current_user.id, parent_id=primary_guest.id,
                               coming_status=primary_guest.coming_status,
                               travel_mode=primary_guest.travel_mode,
                               ticket_status=primary_guest.ticket_status,
                               food_preference=primary_guest.food_preference)
                db.session.add(m_guest)
                
    db.session.commit()
    return jsonify({'success': True, 'guest': guest_to_dict(primary_guest)})

@app.route('/event/<int:event_id>/guests/<int:guest_id>/update', methods=['POST'])
@login_required
def update_guest(event_id, guest_id):
    guest = Guest.query.get_or_404(guest_id)
    if not is_admin_or_manager(event_id, current_user.id) and guest.added_by != current_user.id:
        return jsonify({'error': 'Unauthorized. You can only edit guests you added personally.'}), 403
    
    data = request.get_json()
    
    if 'name' in data:
        guest.name = data['name'].strip()
    
    if 'coming_status' in data:
        guest.coming_status = data['coming_status']
    
    if 'travel_mode' in data:
        guest.travel_mode = data['travel_mode']
    
    if 'ticket_status' in data:
        guest.ticket_status = data['ticket_status']
    
    if 'food_preference' in data:
        guest.food_preference = data['food_preference']

    # Handle Family Syncing
    if guest.is_family and 'family_members' in data and guest.parent_id is None:
        old_members = json.loads(guest.family_members or '[]')
        new_members = [m.strip() for m in data['family_members'] if m.strip()]
        guest.family_members = json.dumps(new_members)
        
        # Sync children records
        existing_children = {c.name.strip(): c for c in guest.children}
        
        # Add or keep
        current_child_names = set()
        for m_name in new_members:
            current_child_names.add(m_name)
            if m_name not in existing_children:
                # Add new child
                new_child = Guest(event_id=event_id, name=m_name, is_family=True,
                                  added_by=current_user.id, parent_id=guest.id,
                                  coming_status=guest.coming_status,
                                  travel_mode=guest.travel_mode,
                                  ticket_status=guest.ticket_status,
                                  food_preference=guest.food_preference)
                db.session.add(new_child)
        
        # Remove those not in new list
        for name, child in existing_children.items():
            if name not in current_child_names:
                db.session.delete(child)

    db.session.commit()

    # If this is a parent, cascade certain updates to children
    if guest.parent_id is None:
        child_updates = {}
        if 'coming_status' in data: child_updates['coming_status'] = data['coming_status']
        if 'travel_mode' in data: child_updates['travel_mode'] = data['travel_mode']
        if 'ticket_status' in data: child_updates['ticket_status'] = data['ticket_status']
        if 'food_preference' in data: child_updates['food_preference'] = data['food_preference']
        
        if child_updates:
            Guest.query.filter_by(parent_id=guest.id).update(child_updates)
            db.session.commit()

    return jsonify({'success': True, 'guest': guest_to_dict(guest)})

@app.route('/event/<int:event_id>/guests/<int:guest_id>/delete', methods=['POST'])
@login_required
def delete_guest(event_id, guest_id):
    guest = Guest.query.get_or_404(guest_id)
    if not is_admin_or_manager(event_id, current_user.id) and guest.added_by != current_user.id:
        return jsonify({'error': 'Unauthorized. You can only delete guests you added personally.'}), 403
    RoomGuest.query.filter_by(guest_id=guest_id).delete()
    db.session.delete(guest)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/event/<int:event_id>/guests/list')
@login_required
def get_guests(event_id):
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    guests = Guest.query.filter_by(event_id=event_id).all()
    return jsonify([guest_to_dict(g) for g in guests])

def guest_to_dict(g):
    return {
        'id': g.id,
        'name': g.name,
        'is_family': g.is_family,
        'family_members': json.loads(g.family_members or '[]'),
        'coming_status': g.coming_status,
        'travel_mode': g.travel_mode,
        'ticket_status': g.ticket_status,
        'food_preference': g.food_preference,
        'parent_id': g.parent_id,
        'parent_name': g.parent.name if g.parent else None,
        'created_at': g.created_at.strftime('%Y-%m-%d')
    }


# ─────────────────────────────────────────────
#  STAY MANAGEMENT
# ─────────────────────────────────────────────

@app.route('/event/<int:event_id>/stay')
@login_required
def stay(event_id):
    event = Event.query.get_or_404(event_id)
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    accommodations = Accommodation.query.filter_by(event_id=event_id).all()
    return render_template('stay.html', event=event, accommodations=accommodations,
                           member=member,
                           is_admin=is_admin(event_id, current_user.id),
                           is_admin_or_manager=is_admin_or_manager(event_id, current_user.id))

@app.route('/event/<int:event_id>/stay/add-place', methods=['POST'])
@login_required
def add_place(event_id):
    if not is_admin_or_manager(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized. Only admins and managers can add accommodations.'}), 403
    data = request.get_json()
    place = Accommodation(event_id=event_id,
                          place_name=data.get('place_name', '').strip(),
                          place_type=data.get('place_type', 'Hotel'))
    db.session.add(place)
    db.session.commit()
    return jsonify({'success': True, 'id': place.id, 'name': place.place_name, 'type': place.place_type})

@app.route('/event/<int:event_id>/stay/add-room', methods=['POST'])
@login_required
def add_room(event_id):
    if not is_admin_or_manager(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized. Only admins and managers can add rooms.'}), 403
    data = request.get_json()
    room = Room(accommodation_id=data.get('accommodation_id'),
                room_number=data.get('room_number', '').strip())
    db.session.add(room)
    db.session.commit()
    return jsonify({'success': True, 'id': room.id, 'room_number': room.room_number})

@app.route('/event/<int:event_id>/stay/assign-guest', methods=['POST'])
@login_required
def assign_guest_to_room(event_id):
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json()
    room_id = data.get('room_id')
    guest_id = data.get('guest_id')
    
    # Remove existing assignment for this guest
    RoomGuest.query.filter_by(guest_id=guest_id).delete()
    rg = RoomGuest(room_id=room_id, guest_id=guest_id)
    db.session.add(rg)
    
    # Auto-assign family members if this is a family head
    guest = Guest.query.get(guest_id)
    if guest and guest.is_family and not guest.parent_id:
        children = Guest.query.filter_by(parent_id=guest.id).all()
        for child in children:
            # Remove child from any other rooms first
            RoomGuest.query.filter_by(guest_id=child.id).delete()
            # Assign to the same room
            child_rg = RoomGuest(room_id=room_id, guest_id=child.id)
            db.session.add(child_rg)
            
    db.session.commit()
    return jsonify({'success': True})

@app.route('/event/<int:event_id>/stay/remove-guest', methods=['POST'])
@login_required
def remove_guest_from_room(event_id):
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    guest_id = data.get('guest_id')
    room_id = data.get('room_id')
    
    guest = Guest.query.get_or_404(guest_id)
    # Permission check: admin/manager OR guest owner
    if not is_admin_or_manager(event_id, current_user.id) and guest.added_by != current_user.id:
        return jsonify({'error': 'Unauthorized. You can only remove guests you added personally.'}), 403
        
    RoomGuest.query.filter_by(guest_id=guest_id, room_id=room_id).delete()
    db.session.commit()
    return jsonify({'success': True})

@app.route('/event/<int:event_id>/stay/data')
@login_required
def stay_data(event_id):
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    
    # Eager load rooms, guests and their assignments to optimize performance
    accommodations = Accommodation.query.options(
        joinedload(Accommodation.rooms).joinedload(Room.room_guests).joinedload(RoomGuest.guest)
    ).filter_by(event_id=event_id).all()
    result = []
    for acc in accommodations:
        rooms_data = []
        for room in acc.rooms:
            assigned = []
            for rg in room.room_guests:
                g = rg.guest
                assigned.append({
                    'id': g.id, 
                    'name': g.name, 
                    'is_family': g.is_family,
                    'added_by': g.added_by
                })
            rooms_data.append({'id': room.id, 'number': room.room_number, 'guests': assigned})
        result.append({'id': acc.id, 'name': acc.place_name, 'type': acc.place_type, 'rooms': rooms_data})
    # Only show guests NOT already assigned to a room
    assigned_guest_ids = [rg.guest_id for rg in RoomGuest.query.join(Room).join(Accommodation).filter(Accommodation.event_id == event_id).all()]
    guests = Guest.query.filter(Guest.event_id == event_id, ~Guest.id.in_(assigned_guest_ids)).all()
    guest_list = [{'id': g.id, 'name': g.name, 'is_family': g.is_family, 'parent_name': g.parent.name if g.parent else None} for g in guests]
    return jsonify({'accommodations': result, 'guests': guest_list})

@app.route('/event/<int:event_id>/stay/delete-place/<int:place_id>', methods=['POST'])
@login_required
def delete_place(event_id, place_id):
    if not is_admin_or_manager(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
    acc = Accommodation.query.get_or_404(place_id)
    for room in acc.rooms:
        RoomGuest.query.filter_by(room_id=room.id).delete()
        db.session.delete(room)
    db.session.delete(acc)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/event/<int:event_id>/stay/delete-room/<int:room_id>', methods=['POST'])
@login_required
def delete_room(event_id, room_id):
    if not is_admin_or_manager(event_id, current_user.id):
        return jsonify({'error': 'Unauthorized'}), 403
    room = Room.query.get_or_404(room_id)
    RoomGuest.query.filter_by(room_id=room_id).delete()
    db.session.delete(room)
    db.session.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────────
#  TRAVEL MANAGEMENT
# ─────────────────────────────────────────────

@app.route('/event/<int:event_id>/travel')
@login_required
def travel(event_id):
    event = Event.query.get_or_404(event_id)
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    if not is_admin(event_id, current_user.id):
        flash('Only admins can manage travel.', 'error')
        return redirect(url_for('event_dashboard', event_id=event_id))
    guests = Guest.query.filter_by(event_id=event_id).all()
    return render_template('travel.html', event=event, guests=guests,
                           member=member,
                           is_admin=True,
                           is_admin_or_manager=True)


# ─────────────────────────────────────────────
#  NOTIFICATIONS
# ─────────────────────────────────────────────

@app.route('/event/<int:event_id>/notifications')
@login_required
def notifications(event_id):
    event = Event.query.get_or_404(event_id)
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    
    # Get direct notifications or broadcasts (receiver_id is NULL)
    notifs = Notification.query.filter(
        Notification.event_id == event_id,
        (Notification.receiver_id == current_user.id) | (Notification.receiver_id == None)
    ).order_by(Notification.created_at.desc()).all()
    
    return render_template('notifications.html', event=event, member=member,
                           notifications=notifs,
                           is_admin=is_admin(event_id, current_user.id),
                           is_admin_or_manager=is_admin_or_manager(event_id, current_user.id))

@app.route('/event/<int:event_id>/notifications/send', methods=['POST'])
@login_required
def send_notification(event_id):
    member = get_member(event_id, current_user.id)
    if not member or member.status != 'approved':
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    message = data.get('message', '').strip()
    receiver_id = data.get('receiver_id') # Can be 'all', 'admins', or specific ID
    
    if not message:
        return jsonify({'error': 'Message cannot be empty'}), 400

    sent_emails = 0
    
    if receiver_id == 'all':
        if member.role != 'admin':
            return jsonify({'error': 'Unauthorized'}), 403
        
        event = Event.query.get(event_id)
        # Eager load user data for all members to optimize email loop
        event_members = EventMember.query.options(joinedload(EventMember.user)) \
            .filter_by(event_id=event_id, status='approved').all()
            
        for em in event_members:
            if em.user_id != current_user.id:
                notif = Notification(event_id=event_id, sender_id=current_user.id, 
                                     receiver_id=em.user_id, message=message)
                db.session.add(notif)
                email_sent = send_notification_email(em.user.email, f"New Notification: {event.name}", 
                                        render_template('emails/notification_email.html', 
                                                        user=em.user, event=event, message=message, sender=current_user))
                if email_sent: sent_emails += 1
    
    elif receiver_id == 'admins':
        # Members/Managers sending to all admins
        admins = EventMember.query.filter_by(event_id=event_id, role='admin', status='approved').all()
        for adm in admins:
            if adm.user_id != current_user.id:
                notif = Notification(event_id=event_id, sender_id=current_user.id, 
                                     receiver_id=adm.user_id, message=message)
                db.session.add(notif)
                email_sent = send_notification_email(adm.user.email, f"Admin Alert: {Event.query.get(event_id).name}", 
                                        render_template('emails/notification_email.html', 
                                                        user=adm.user, event=adm.event, message=message, sender=current_user))
                if email_sent: sent_emails += 1
    
    else:
        # Single recipient
        target_user = User.query.get_or_404(receiver_id)
        notif = Notification(event_id=event_id, sender_id=current_user.id, 
                             receiver_id=target_user.id, message=message)
        db.session.add(notif)
        email_sent = send_notification_email(target_user.email, f"New Message from {current_user.name}", 
                                render_template('emails/notification_email.html', 
                                                user=target_user, event=Event.query.get(event_id), message=message, sender=current_user))
        if email_sent: sent_emails += 1

    db.session.commit()
    return jsonify({'success': True, 'emails_sent': sent_emails})

@app.route('/event/<int:event_id>/notifications/mark-read', methods=['POST'])
@login_required
def mark_notifications_read(event_id):
    Notification.query.filter_by(event_id=event_id, receiver_id=current_user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})

@app.route('/event/<int:event_id>/notifications/unread-count')
@login_required
def unread_count(event_id):
    count = Notification.query.filter_by(event_id=event_id, receiver_id=current_user.id, is_read=False).count()
    return jsonify({'count': count})


if __name__ == '__main__':
    print('\n  [OK]  EventFlow is running at http://127.0.0.1:5000\n')
    app.run(debug=True, port=5000)
