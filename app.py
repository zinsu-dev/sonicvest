from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, has_request_context
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dotenv import load_dotenv
import os
import secrets
import re
import threading
import time

# Resolve project root early and force-load .env from this folder.
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'), override=True)

# Import admin blueprint
from admin_routes import admin_bp

# Import admin DB helpers
from admin_db import execute_one, execute_insert, execute_update

# Import GTR Pay service
from gtr_services_clean import GTRPayService
gtr_pay_service = GTRPayService()

# Default system settings (will be updated from DB later)
SYSTEM_SETTINGS = {
    'WELCOME_BONUS': 100.0,
    'MINIMUM_DEPOSIT': 3000.0,
    'MINIMUM_WITHDRAWAL': 2200.0,
    'DAILY_CHECKIN_BONUS': 100.0,
    'WITHDRAWAL_FEE_PERCENTAGE': 11.0,
    'INCOME_DROP_HOURS': 24.0,
    'WITHDRAWAL_START_TIME': 9,
    'WITHDRAWAL_END_TIME': 17,
    'REFERRAL_COMMISSIONS': {
        'LEVEL_1': 0.15,
        'LEVEL_2': 0.03,
        'LEVEL_3': 0.01
    }
}


def utc_now():
    """Return current UTC time as naive datetime for legacy DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_withdrawal_timezone():
    """Return the timezone used for withdrawal window checks."""
    timezone_name = os.environ.get('WITHDRAWAL_TIMEZONE', 'Africa/Lagos')
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


WITHDRAWAL_TIMEZONE = _load_withdrawal_timezone()


def format_withdrawal_window(start_hour, end_hour):
    """Format stored 24-hour withdrawal settings to match the admin panel."""
    start_hour = int(start_hour) % 24
    end_hour = int(end_hour) % 24
    timezone_label = datetime.now(WITHDRAWAL_TIMEZONE).tzname() or WITHDRAWAL_TIMEZONE.key
    return f"{start_hour:02d}:00 - {end_hour:02d}:00 {timezone_label}"


def current_withdrawal_hour():
    """Return the current hour in the configured withdrawal timezone."""
    return datetime.now(WITHDRAWAL_TIMEZONE).hour


def is_withdrawal_window_open(start_hour, end_hour, current_hour=None):
    """Return True when withdrawals are currently allowed."""
    if current_hour is None:
        current_hour = current_withdrawal_hour()

    start_hour = int(start_hour) % 24
    end_hour = int(end_hour) % 24
    current_hour = int(current_hour) % 24

    if start_hour == end_hour:
        return False

    if start_hour < end_hour:
        return start_hour <= current_hour < end_hour

    return current_hour >= start_hour or current_hour < end_hour

# Initialize Flask app with explicit static folder configuration
app = Flask(__name__,
           static_folder=os.path.join(BASE_DIR, 'static'),
           static_url_path='/static')

# Security: Use environment variable for SECRET_KEY in production
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'your-secret-key-change-this-in-production'

# Database configuration - prefer PostgreSQL via DATABASE_URL, fallback to local SQLite
database_url = (os.environ.get('DATABASE_URL') or '').strip()
if database_url:
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    print(f"✅ Using PostgreSQL database: {database_url[:60]}...")
else:
    database_url = f'sqlite:///{os.path.join(BASE_DIR, "maxwealth.db")}'
    print(f"⚠️ DATABASE_URL not set, using SQLite database: {database_url}")

app.config['SQLALCHEMY_DATABASE_URI'] = database_url

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Database connection pool configuration - only for PostgreSQL
if 'sqlite' not in database_url:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_size': 10,
        'pool_recycle': 3600,
        'pool_pre_ping': True,
        'max_overflow': 20,
        'pool_timeout': 30,
        'connect_args': {
            'connect_timeout': 10,
            'keepalives': 1,
            'keepalives_idle': 30,
            'keepalives_interval': 10,
            'keepalives_count': 5
        }
    }

db = SQLAlchemy(app)

# Register admin blueprint
# ---------------------------------------------------------------------------
# Flask-Mail configuration
# ---------------------------------------------------------------------------
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() in ('true', '1', 'on')

# Read and normalize SMTP settings to avoid invalid sender/auth combinations.
mail_username = (os.environ.get('MAIL_USERNAME') or '').strip()
mail_password = (os.environ.get('MAIL_PASSWORD') or '').strip()
mail_sender_name = (os.environ.get('MAIL_SENDER_NAME') or 'SONICVEST').strip()
mail_sender_email = (os.environ.get('MAIL_SENDER_EMAIL') or mail_username).strip()

app.config['MAIL_USERNAME'] = mail_username or None
app.config['MAIL_PASSWORD'] = mail_password or None
if mail_sender_email:
    app.config['MAIL_DEFAULT_SENDER'] = (mail_sender_name, mail_sender_email)

# Safe startup diagnostics (no secrets) for SMTP setup.
if app.config.get('MAIL_USERNAME') and app.config.get('MAIL_PASSWORD'):
    print("📧 Mail configuration detected.")
else:
    print("⚠️  Mail not configured: set MAIL_USERNAME and MAIL_PASSWORD in .env")

mail = Mail(app)


def build_public_url(endpoint: str, **values) -> str:
    """Build a public absolute URL, preferring APP_BASE_URL when configured."""
    public_base_url = os.environ.get('APP_BASE_URL', '').strip().rstrip('/')
    relative_url = url_for(endpoint, **values)
    if public_base_url:
        return f"{public_base_url}{relative_url}"
    return url_for(endpoint, _external=True, **values)


def send_welcome_email(user_name: str, user_email: str, login_url: str):
    """Send a welcome email to a newly registered user (non-blocking)."""
    def _send():
        try:
            if not app.config.get('MAIL_USERNAME') or not app.config.get('MAIL_PASSWORD'):
                print("⚠️  Welcome email skipped: MAIL_USERNAME or MAIL_PASSWORD is missing.")
                return

            with app.app_context():
                html_body = render_template(
                    'emails/welcome.html',
                    name=user_name,
                    email=user_email,
                    welcome_bonus=f"{SYSTEM_SETTINGS['WELCOME_BONUS']:,.0f}",
                    login_url=login_url
                )
                text_body = render_template(
                    'emails/welcome.txt',
                    name=user_name,
                    email=user_email,
                    welcome_bonus=f"{SYSTEM_SETTINGS['WELCOME_BONUS']:,.0f}",
                    login_url=login_url
                )
                msg = Message(
                    subject='Welcome to SONICVEST - Account created',
                    recipients=[user_email],
                    sender=app.config.get('MAIL_DEFAULT_SENDER'),
                    body=text_body,
                    html=html_body
                )
                mail.send(msg)
                print(f"✅ Welcome email sent to {user_email}")
        except Exception as exc:
            print(f"⚠️  Could not send welcome email to {user_email}: {exc}")
            if '5.7.0' in str(exc) or 'Authentication Required' in str(exc):
                print("ℹ️  Gmail SMTP auth failed. Use a valid Gmail address in MAIL_USERNAME and a Gmail App Password in MAIL_PASSWORD.")
                print("ℹ️  Also set MAIL_SENDER_EMAIL to the same Gmail address or an authorized sender alias.")

    t = threading.Thread(target=_send, daemon=True)
    t.start()


def send_package_purchase_email(
    user_name: str,
    user_email: str,
    package_name: str,
    package_price: float,
    daily_income: float,
    duration_days: int,
    total_return: float,
    purchase_date: str,
    dashboard_url: str,
):
    """Send a package purchase confirmation email (non-blocking)."""
    def _send():
        try:
            if not app.config.get('MAIL_USERNAME') or not app.config.get('MAIL_PASSWORD'):
                print("⚠️  Package purchase email skipped: MAIL_USERNAME or MAIL_PASSWORD is missing.")
                return

            with app.app_context():
                html_body = render_template(
                    'emails/package_purchase.html',
                    name=user_name,
                    email=user_email,
                    package_name=package_name,
                    package_price=f"{package_price:,.0f}",
                    daily_income=f"{daily_income:,.0f}",
                    duration_days=duration_days,
                    total_return=f"{total_return:,.0f}",
                    purchase_date=purchase_date,
                    dashboard_url=dashboard_url
                )
                text_body = render_template(
                    'emails/package_purchase.txt',
                    name=user_name,
                    email=user_email,
                    package_name=package_name,
                    package_price=f"{package_price:,.0f}",
                    daily_income=f"{daily_income:,.0f}",
                    duration_days=duration_days,
                    total_return=f"{total_return:,.0f}",
                    purchase_date=purchase_date,
                    dashboard_url=dashboard_url
                )
                msg = Message(
                    subject=f'Package purchase confirmed - {package_name}',
                    recipients=[user_email],
                    sender=app.config.get('MAIL_DEFAULT_SENDER'),
                    body=text_body,
                    html=html_body
                )
                mail.send(msg)
                print(f"✅ Package purchase email sent to {user_email}")
        except Exception as exc:
            print(f"⚠️  Could not send package purchase email to {user_email}: {exc}")
            if '5.7.0' in str(exc) or 'Authentication Required' in str(exc):
                print("ℹ️  Gmail SMTP auth failed. Use a valid Gmail address in MAIL_USERNAME and a Gmail App Password in MAIL_PASSWORD.")
                print("ℹ️  Also set MAIL_SENDER_EMAIL to the same Gmail address or an authorized sender alias.")

    t = threading.Thread(target=_send, daemon=True)
    t.start()


def send_withdrawal_requested_email(
    user_name: str,
    user_email: str,
    amount: float,
    fee: float,
    fee_percentage: float,
    net_amount: float,
    bank_name: str,
    account_number: str,
    account_name: str,
    request_date: str,
    dashboard_url: str,
):
    """Send withdrawal request received email (non-blocking)."""
    def _send():
        try:
            if not app.config.get('MAIL_USERNAME') or not app.config.get('MAIL_PASSWORD'):
                print("⚠️  Withdrawal requested email skipped: MAIL_USERNAME or MAIL_PASSWORD is missing.")
                return

            with app.app_context():
                html_body = render_template(
                    'emails/withdrawal_requested.html',
                    name=user_name,
                    email=user_email,
                    amount=f"{amount:,.0f}",
                    fee=f"{fee:,.0f}",
                    fee_percentage=f"{fee_percentage:.0f}",
                    net_amount=f"{net_amount:,.0f}",
                    bank_name=bank_name,
                    account_number=account_number,
                    account_name=account_name,
                    request_date=request_date,
                    dashboard_url=dashboard_url,
                )
                text_body = render_template(
                    'emails/withdrawal_requested.txt',
                    name=user_name,
                    email=user_email,
                    amount=f"{amount:,.0f}",
                    fee=f"{fee:,.0f}",
                    fee_percentage=f"{fee_percentage:.0f}",
                    net_amount=f"{net_amount:,.0f}",
                    bank_name=bank_name,
                    account_number=account_number,
                    account_name=account_name,
                    request_date=request_date,
                    dashboard_url=dashboard_url,
                )
                msg = Message(
                    subject='Withdrawal request received - Under review',
                    recipients=[user_email],
                    sender=app.config.get('MAIL_DEFAULT_SENDER'),
                    body=text_body,
                    html=html_body,
                )
                mail.send(msg)
                print(f"✅ Withdrawal requested email sent to {user_email}")
        except Exception as exc:
            print(f"⚠️  Could not send withdrawal requested email to {user_email}: {exc}")

    t = threading.Thread(target=_send, daemon=True)
    t.start()


def send_withdrawal_approved_email(
    user_name: str,
    user_email: str,
    amount: float,
    net_amount: float,
    bank_name: str,
    account_number: str,
    approval_date: str,
    trace_id: str,
    dashboard_url: str,
):
    """Send withdrawal approved email (non-blocking)."""
    def _send():
        try:
            if not app.config.get('MAIL_USERNAME') or not app.config.get('MAIL_PASSWORD'):
                print("⚠️  Withdrawal approved email skipped: MAIL_USERNAME or MAIL_PASSWORD is missing.")
                return

            with app.app_context():
                html_body = render_template(
                    'emails/withdrawal_approved.html',
                    name=user_name,
                    email=user_email,
                    amount=f"{amount:,.0f}",
                    net_amount=f"{net_amount:,.0f}",
                    bank_name=bank_name,
                    account_number=account_number,
                    approval_date=approval_date,
                    trace_id=trace_id,
                    dashboard_url=dashboard_url,
                )
                text_body = render_template(
                    'emails/withdrawal_approved.txt',
                    name=user_name,
                    email=user_email,
                    amount=f"{amount:,.0f}",
                    net_amount=f"{net_amount:,.0f}",
                    bank_name=bank_name,
                    account_number=account_number,
                    approval_date=approval_date,
                    trace_id=trace_id,
                    dashboard_url=dashboard_url,
                )
                msg = Message(
                    subject='Withdrawal approved - Funds on the way',
                    recipients=[user_email],
                    sender=app.config.get('MAIL_DEFAULT_SENDER'),
                    body=text_body,
                    html=html_body,
                )
                mail.send(msg)
                print(f"✅ Withdrawal approved email sent to {user_email}")
        except Exception as exc:
            print(f"⚠️  Could not send withdrawal approved email to {user_email}: {exc}")

    t = threading.Thread(target=_send, daemon=True)
    t.start()


def send_withdrawal_declined_email(
    user_name: str,
    user_email: str,
    amount: float,
    decline_date: str,
    decline_reason: str,
    dashboard_url: str,
):
    """Send withdrawal declined email (non-blocking)."""
    def _send():
        try:
            if not app.config.get('MAIL_USERNAME') or not app.config.get('MAIL_PASSWORD'):
                print("⚠️  Withdrawal declined email skipped: MAIL_USERNAME or MAIL_PASSWORD is missing.")
                return

            with app.app_context():
                html_body = render_template(
                    'emails/withdrawal_declined.html',
                    name=user_name,
                    email=user_email,
                    amount=f"{amount:,.0f}",
                    decline_date=decline_date,
                    decline_reason=decline_reason,
                    dashboard_url=dashboard_url,
                )
                text_body = render_template(
                    'emails/withdrawal_declined.txt',
                    name=user_name,
                    email=user_email,
                    amount=f"{amount:,.0f}",
                    decline_date=decline_date,
                    decline_reason=decline_reason,
                    dashboard_url=dashboard_url,
                )
                msg = Message(
                    subject='Withdrawal declined - Refunded to your account',
                    recipients=[user_email],
                    sender=app.config.get('MAIL_DEFAULT_SENDER'),
                    body=text_body,
                    html=html_body,
                )
                mail.send(msg)
                print(f"✅ Withdrawal declined email sent to {user_email}")
        except Exception as exc:
            print(f"⚠️  Could not send withdrawal declined email to {user_email}: {exc}")

    t = threading.Thread(target=_send, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
app.register_blueprint(admin_bp)

# Initialize admin routes with database connection
try:
    from admin_routes import init_admin_routes
    init_admin_routes(db)
except Exception as e:
    print(f"⚠️  Admin routes initialization: {e}")

# Initialize database before first request
@app.before_request
def initialize_database():
    """Ensure database is created before handling any request"""
    try:
        # Remove this function after first execution
        app.before_request_funcs[None].remove(initialize_database)
        
        with app.app_context():
            # Create all tables if they don't exist
            db.create_all()
            print("✅ Database tables ensured")

            ensure_system_settings_schema()
            ensure_withdrawal_schema()
            
            # Load system settings from DB
            SYSTEM_SETTINGS = load_system_settings()
            print("✅ System settings loaded from DB")
            
            # Initialize admin database - CREATE TABLES FIRST
            try:
                from sqlalchemy import text
                
                # Create admin_users table
                try:
                    db.session.execute(text('''
                        CREATE TABLE IF NOT EXISTS admin_users (
                            id SERIAL PRIMARY KEY,
                            username VARCHAR(100) UNIQUE NOT NULL,
                            email VARCHAR(100) UNIQUE NOT NULL,
                            password_hash TEXT NOT NULL,
                            role VARCHAR(50) DEFAULT 'admin',
                            is_active BOOLEAN DEFAULT true,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            last_login TIMESTAMP
                        )
                    '''))
                    db.session.commit()
                    print("✅ Admin users table created")
                except Exception as e:
                    db.session.rollback()
                    print(f"⚠️  Admin users table: {e}")
                
                # Create admin_logs table
                try:
                    db.session.execute(text('''
                        CREATE TABLE IF NOT EXISTS admin_logs (
                            id SERIAL PRIMARY KEY,
                            admin_id INTEGER,
                            action VARCHAR(255) NOT NULL,
                            target_type VARCHAR(100),
                            target_id INTEGER,
                            details TEXT,
                            ip_address VARCHAR(45),
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    '''))
                    db.session.commit()
                    print("✅ Admin logs table created")
                except Exception as e:
                    db.session.rollback()
                    print(f"⚠️  Admin logs table: {e}")
                
                # Create audit_logs table
                try:
                    db.session.execute(text('''
                        CREATE TABLE IF NOT EXISTS audit_logs (
                            id SERIAL PRIMARY KEY,
                            user_id INTEGER,
                            action VARCHAR(255) NOT NULL,
                            resource_type VARCHAR(100),
                            resource_id INTEGER,
                            changes TEXT,
                            ip_address VARCHAR(45),
                            user_agent TEXT,
                            status VARCHAR(50),
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    '''))
                    db.session.commit()
                    print("✅ Audit logs table created")
                except Exception as e:
                    db.session.rollback()
                    print(f"⚠️  Audit logs table: {e}")
                
                # Create bank_details table
                try:
                    db.session.execute(text('''
                        CREATE TABLE IF NOT EXISTS bank_details (
                            id SERIAL PRIMARY KEY,
                            bank_name VARCHAR(100),
                            account_number VARCHAR(50),
                            account_name VARCHAR(100),
                            is_active BOOLEAN DEFAULT 1,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    '''))
                    db.session.commit()
                    print("✅ Bank details table created")
                except Exception as e:
                    db.session.rollback()
                    print(f"⚠️  Bank details table: {e}")
                
                # Create system_settings table
                try:
                    db.session.execute(text('''
                        CREATE TABLE IF NOT EXISTS system_settings (
                            id SERIAL PRIMARY KEY,
                            welcome_bonus REAL DEFAULT 100.0,
                            minimum_deposit REAL DEFAULT 3000.0,
                            minimum_withdrawal REAL DEFAULT 2200.0,
                            daily_checkin_bonus REAL DEFAULT 20.0,
                            withdrawal_fee_percentage REAL DEFAULT 11.0,
                            income_drop_hours REAL DEFAULT 24.0,
                            withdrawal_start_time INTEGER DEFAULT 9,
                            withdrawal_end_time INTEGER DEFAULT 17,
                            referral_level1 REAL DEFAULT 0.15,
                            referral_level2 REAL DEFAULT 0.03,
                            referral_level3 REAL DEFAULT 0.01,
                            deposit_gateway_mode VARCHAR(20) DEFAULT 'automatic',
                            is_active BOOLEAN DEFAULT 1,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    '''))
                    db.session.commit()
                    print("✅ System settings table created")
                except Exception as e:
                    db.session.rollback()
                    print(f"⚠️  System settings table: {e}")
                
                # Now initialize admin database with default user
                from admin_routes import init_admin_db
                init_admin_db()
                print("✅ Admin database initialized")
            except Exception as e:
                print(f"⚠️  Admin DB initialization: {e}")
            
            # Add missing columns if database exists (migration) - only for SQLite
            database_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
            if 'sqlite' in database_uri:
                try:
                    import sqlite3
                    # Get the database path from URI
                    db_path = database_uri.replace('sqlite:///', '')
                    
                    if os.path.exists(db_path):
                        conn = sqlite3.connect(db_path)
                        cursor = conn.cursor()
                        
                        # Migrate user table
                        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user'")
                        if cursor.fetchone():
                            cursor.execute("PRAGMA table_info(user)")
                            columns = [col[1] for col in cursor.fetchall()]
                            
                            # Add missing columns to user table
                            user_migrations = [
                                ('is_admin', 'BOOLEAN DEFAULT 0'),
                                ('bank_name', 'VARCHAR(100)'),
                                ('account_number', 'VARCHAR(50)'),
                                ('account_name', 'VARCHAR(100)'),
                                ('last_checkin', 'DATETIME')
                            ]
                            
                            for col_name, col_type in user_migrations:
                                if col_name not in columns:
                                    try:
                                        cursor.execute(f"ALTER TABLE user ADD COLUMN {col_name} {col_type}")
                                        print(f"✅ Added user.{col_name}")
                                    except Exception as col_error:
                                        pass  # Column might already exist
                        
                        # Migrate user_package table
                        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_package'")
                        if cursor.fetchone():
                            cursor.execute("PRAGMA table_info(user_package)")
                            columns = [col[1] for col in cursor.fetchall()]
                            
                            # Add missing columns to user_package table
                            package_migrations = [
                                ('purchase_date', 'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP'),
                                ('expiry_date', 'DATETIME'),
                                ('amount_invested', 'FLOAT DEFAULT 0'),
                                ('start_date', 'DATETIME DEFAULT CURRENT_TIMESTAMP'),
                                ('end_date', 'DATETIME'),
                                ('daily_return', 'FLOAT DEFAULT 0'),
                                ('total_earned', 'FLOAT DEFAULT 0'),
                                ('last_payout', 'DATETIME'),
                                ('is_active', 'BOOLEAN DEFAULT 1')
                            ]
                            
                            for col_name, col_type in package_migrations:
                                if col_name not in columns:
                                    try:
                                        cursor.execute(f"ALTER TABLE user_package ADD COLUMN {col_name} {col_type}")
                                        print(f"✅ Added user_package.{col_name}")
                                    except Exception as col_error:
                                        pass  # Column might already exist
                        
                        conn.commit()
                        conn.close()
                except Exception as migration_e:
                    print(f"⚠️  Migration: {migration_e}")
    except Exception as e:
        print(f"⚠️  Database initialization skipped: {e}")

# Custom Jinja2 filters
@app.template_filter('format_number')
def format_number(value):
    try:
        return "{:,.0f}".format(float(value))
    except (ValueError, TypeError):
        return "0"

@app.template_filter('format_date')
def format_date(value, fmt='%Y-%m-%d'):
    """Safely format a date, whether it's a datetime object or string"""
    from datetime import datetime
    if not value:
        return ''
    if isinstance(value, datetime):
        return value.strftime(fmt)
    if isinstance(value, str):
        # If it's already a string, just take the first 10 chars if it's an ISO string
        return value[:10]
    return ''

# Add context processors to make variables available in all templates
@app.context_processor
def inject_now():
    return {'now': utc_now()}

@app.context_processor
def inject_user():
    # Email/template rendering in background threads has no request/session context.
    if not has_request_context():
        return {'current_user': None}
    return {'current_user': get_current_user()}

# --- Referral commission helper ---
def distribute_referral_commissions(buyer, purchase_amount, source_desc=None):
    """Distribute referral commissions up to 3 levels based on a purchase amount.

    Level 1: 10%, Level 2: 1%, Level 3: 1% (from SYSTEM_SETTINGS['REFERRAL_COMMISSIONS']).
    Credits referrers' main_balance and logs Transaction(type='referral_bonus').
    """
    try:
        rates = SYSTEM_SETTINGS.get('REFERRAL_COMMISSIONS', {})

        def get_user(user_id):
            return User.query.get(user_id) if user_id else None

        level1 = get_user(buyer.referred_by)
        level2 = get_user(level1.referred_by) if level1 else None
        level3 = get_user(level2.referred_by) if level2 else None

        chain = [
            (1, level1, float(rates.get('LEVEL_1', 0))),
            (2, level2, float(rates.get('LEVEL_2', 0))),
            (3, level3, float(rates.get('LEVEL_3', 0))),
        ]

        for lvl, ref_user, rate in chain:
            if not ref_user or rate <= 0:
                continue
            commission = round(float(purchase_amount) * rate, 2)
            ref_user.main_balance = (ref_user.main_balance or 0) + commission
            desc = (
                f"Level {lvl} referral commission from {(buyer.name or buyer.phone or 'user')}\'s {source_desc or 'purchase'}: ₦{commission:,.2f}"
            )
            db.session.add(Transaction(
                user_id=ref_user.id,
                type='referral_bonus',
                amount=commission,
                description=desc,
                status='completed'
            ))
    except Exception as e:
        # Non-fatal: do not block purchase on referral payout error
        print(f"Error distributing referral commissions: {e}")

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    referral_code = db.Column(db.String(20), unique=True, nullable=False)
    referred_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    recharge_balance = db.Column(db.Float, default=0.0)
    main_balance = db.Column(db.Float, default=0.0)
    total_earned = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=utc_now)
    is_active = db.Column(db.Boolean, default=True)
    is_admin = db.Column(db.Boolean, default=False)  # Admin field
    
    # Bank details fields
    bank_name = db.Column(db.String(100), nullable=True)
    account_number = db.Column(db.String(20), nullable=True)
    account_name = db.Column(db.String(100), nullable=True)
    
    # Daily check-in tracking
    last_checkin = db.Column(db.DateTime, nullable=True)
    
    def __init__(self, **kwargs):
        super(User, self).__init__(**kwargs)
        if not self.referral_code:
            self.referral_code = self.generate_referral_code()
    
    def generate_referral_code(self):
        """Generate a unique 8-character referral code"""
        while True:
            code = secrets.token_urlsafe(6)[:8].upper()
            if not User.query.filter_by(referral_code=code).first():
                return code
    
    @property
    def balance(self):
        """Total available balance (recharge + main)"""
        return (self.recharge_balance or 0) + (self.main_balance or 0)
    
    @property
    def username(self):
        """Alias for name field to match admin templates"""
        return self.name
    
    @property
    def full_name(self):
        """Alias for name field to match admin templates"""
        return self.name
    
    @property
    def withdrawable_balance(self):
        """Only main balance can be withdrawn"""
        return self.main_balance or 0
    
    @property
    def total_earned_property(self):
        """Calculate total earnings from all sources"""
        # This will be calculated dynamically when needed
        # to avoid circular imports during model definition
        return self.main_balance or 0
    
    def get_referral_count(self):
        """Get the total number of direct referrals"""
        return User.query.filter_by(referred_by=self.id).count()
    
    def get_level_referrals(self, level):
        """Get referrals at a specific level"""
        if level == 1:
            return User.query.filter_by(referred_by=self.id).all()
        elif level == 2:
            level1_users = User.query.filter_by(referred_by=self.id).all()
            level2_users = []
            for user in level1_users:
                level2_users.extend(User.query.filter_by(referred_by=user.id).all())
            return level2_users
        elif level == 3:
            level1_users = User.query.filter_by(referred_by=self.id).all()
            level2_users = []
            for user in level1_users:
                level2_users.extend(User.query.filter_by(referred_by=user.id).all())
            level3_users = []
            for user in level2_users:
                level3_users.extend(User.query.filter_by(referred_by=user.id).all())
            return level3_users
        return []

class Package(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    price = db.Column(db.Float, nullable=False)
    daily_return_rate = db.Column(db.Float, default=2.86)  # 2.86% daily
    duration_days = db.Column(db.Integer, default=30)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    @property
    def daily_income(self):
        """Calculate daily income based on price and daily return rate"""
        return (self.price * self.daily_return_rate) / 100
    
    @property
    def total_return(self):
        """Calculate total return over the duration"""
        return self.daily_income * self.duration_days
    
    @property
    def total_income(self):
        """Alias for total_return (used by templates)"""
        return self.total_return
    
    def get_roi_percentage(self):
        """Calculate ROI percentage over the duration"""
        if self.price == 0:
            return 0
        return (self.total_return / self.price) * 100
    
    @property
    def duration(self):
        """Alias for duration_days (used by templates)"""
        return self.duration_days
    
    @property
    def roi_percentage(self):
        """Calculate ROI percentage over the duration"""
        return (self.total_return / self.price) * 100
    
    @property
    def total_income(self):
        """Alias for total_return for template compatibility"""
        return self.total_return
    
    def get_roi_percentage(self):
        """Method version of roi_percentage for template compatibility"""
        return self.roi_percentage
    
    @property
    def duration(self):
        """Alias for duration_days for template compatibility"""
        return self.duration_days

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # deposit, withdrawal, interest, referral
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(20), default='pending')  # pending, completed, failed
    created_at = db.Column(db.DateTime, default=utc_now)
    reference = db.Column(db.String(100), nullable=True)
    
    user = db.relationship('User', backref='transactions')

class UserPackage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    package_id = db.Column(db.Integer, db.ForeignKey('package.id'), nullable=False)
    # Legacy columns retained in DB; map them to avoid NOT NULL issues
    purchase_date = db.Column(db.DateTime, nullable=False)
    expiry_date = db.Column(db.DateTime, nullable=False)
    amount_invested = db.Column(db.Float, nullable=False)
    start_date = db.Column(db.DateTime, default=utc_now)
    end_date = db.Column(db.DateTime, nullable=False)
    daily_return = db.Column(db.Float, nullable=False)
    total_earned = db.Column(db.Float, default=0.0)
    last_payout = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    
    # Relationships
    package = db.relationship('Package', backref='user_packages')
    user = db.relationship('User', backref='user_packages_relation')
    
    
    @property
    def status(self):
        """Get current status based on dates and is_active"""
        from datetime import datetime
        now = utc_now()
        if not self.is_active:
            return 'completed'
        elif now > self.end_date:
            return 'expired'
        elif now >= self.start_date:
            return 'active'
        else:
            return 'pending'
    
    def get_earnings_to_date(self):
        """Calculate earnings up to current date"""
        from datetime import datetime
        now = utc_now()
        if now < self.start_date:
            return 0.0
        
        if now >= self.end_date:
            # Full investment period completed
            days_completed = (self.end_date - self.start_date).days
        else:
            # Partial period
            days_completed = (now - self.start_date).days
        
        return days_completed * self.daily_return

class Withdrawal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    fee = db.Column(db.Float, nullable=False)
    net_amount = db.Column(db.Float, nullable=False)
    bank_name = db.Column(db.String(100), nullable=False)
    other_bank = db.Column(db.String(100), nullable=True)
    account_number = db.Column(db.String(50), nullable=False)
    account_name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected, processed
    reason = db.Column(db.String(200), nullable=True)  # For rejection reason
    created_at = db.Column(db.DateTime, default=utc_now)
    processed_at = db.Column(db.DateTime, nullable=True)
    processed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)  # Admin who processed
    
    # Relationships
    user = db.relationship('User', foreign_keys=[user_id], backref='withdrawals')
    processor = db.relationship('User', foreign_keys=[processed_by])

class BankDetails(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bank_name = db.Column(db.String(100), nullable=False)
    account_number = db.Column(db.String(50), nullable=False)
    account_name = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utc_now)

class SystemSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    welcome_bonus = db.Column(db.Float, default=100.0)
    minimum_deposit = db.Column(db.Float, default=3000.0)
    minimum_withdrawal = db.Column(db.Float, default=2200.0)
    daily_checkin_bonus = db.Column(db.Float, default=100.0)
    withdrawal_fee_percentage = db.Column(db.Float, default=11.0)
    income_drop_hours = db.Column(db.Float, default=24.0)
    withdrawal_start_time = db.Column(db.Integer, default=9)
    withdrawal_end_time = db.Column(db.Integer, default=17)
    referral_level1 = db.Column(db.Float, default=0.15)
    referral_level2 = db.Column(db.Float, default=0.03)
    referral_level3 = db.Column(db.Float, default=0.01)
    deposit_gateway_mode = db.Column(db.String(20), default='automatic')
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utc_now)

# Function to load system settings from database
def load_system_settings():
    """Load system settings from SystemSettings table"""
    try:
        from flask import has_app_context
        if has_app_context():
            settings_row = execute_one('SELECT * FROM system_settings LIMIT 1')
            if settings_row:
                deposit_gateway_mode = (settings_row.get('deposit_gateway_mode') or 'automatic').strip().lower()
                return {
                    'WELCOME_BONUS': float(settings_row.get('welcome_bonus', 100.0) or 100.0),
                    'MINIMUM_DEPOSIT': float(settings_row.get('minimum_deposit', 3000.0) or 3000.0),
                    'MINIMUM_WITHDRAWAL': float(settings_row.get('minimum_withdrawal', 2200.0) or 2200.0),
                    'DAILY_CHECKIN_BONUS': float(settings_row.get('daily_checkin_bonus', 100.0) or 100.0),
                    'WITHDRAWAL_FEE_PERCENTAGE': float(settings_row.get('withdrawal_fee_percentage', 11.0) or 11.0),
                    'INCOME_DROP_HOURS': float(settings_row.get('income_drop_hours', 24.0) or 24.0),
                    'WITHDRAWAL_START_TIME': int(settings_row.get('withdrawal_start_time', 9) or 9),
                    'WITHDRAWAL_END_TIME': int(settings_row.get('withdrawal_end_time', 17) or 17),
                    'DEPOSIT_GATEWAY_MODE': deposit_gateway_mode if deposit_gateway_mode in ('manual', 'automatic') else 'automatic',
                    'REFERRAL_COMMISSIONS': {
                        'LEVEL_1': float(settings_row.get('referral_level1', 0.15) or 0.15),
                        'LEVEL_2': float(settings_row.get('referral_level2', 0.03) or 0.03),
                        'LEVEL_3': float(settings_row.get('referral_level3', 0.01) or 0.01)
                    }
                }
    except Exception as e:
        print(f"⚠️  Could not load system settings from DB: {e}")
    
    # Default settings if DB fails
    return {
        'WELCOME_BONUS': 100.0,
        'MINIMUM_DEPOSIT': 3000.0,
        'MINIMUM_WITHDRAWAL': 2200.0,
        'DAILY_CHECKIN_BONUS': 100.0,
        'WITHDRAWAL_FEE_PERCENTAGE': 11.0,
        'INCOME_DROP_HOURS': 24.0,
        'WITHDRAWAL_START_TIME': 9,
        'WITHDRAWAL_END_TIME': 17,
        'DEPOSIT_GATEWAY_MODE': 'automatic',
        'REFERRAL_COMMISSIONS': {
            'LEVEL_1': 0.15,
            'LEVEL_2': 0.03,
            'LEVEL_3': 0.01
        }
    }


def ensure_system_settings_schema():
    """Add missing system_settings columns on existing databases."""
    try:
        from sqlalchemy import text
        db.session.execute(text("ALTER TABLE system_settings ADD COLUMN deposit_gateway_mode VARCHAR(20) DEFAULT 'automatic'"))
        db.session.commit()
        print("✅ System settings gateway mode column ensured")
    except Exception as e:
        db.session.rollback()
        message = str(e).lower()
        if 'duplicate column' not in message and 'already exists' not in message:
            print(f"⚠️  System settings schema check: {e}")

def ensure_withdrawal_schema():
    """Add missing withdrawal columns on existing databases."""
    try:
        from sqlalchemy import text
        db.session.execute(text("ALTER TABLE withdrawal ADD COLUMN other_bank VARCHAR(100)"))
        db.session.commit()
        print("✅ Withdrawal custom bank column ensured")
    except Exception as e:
        db.session.rollback()
        message = str(e).lower()
        if 'duplicate column' not in message and 'already exists' not in message:
            print(f"⚠️  Withdrawal schema check: {e}")

# Helper functions
def get_current_user():
    """Get current logged-in user"""
    if 'user_id' in session:
        return User.query.get(session['user_id'])
    return None

def require_login(f):
    """Decorator to require login for routes"""
    def decorated_function(*args, **kwargs):
        if not get_current_user():
            flash('Please login to access this page', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# Routes
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        return register()
    return render_template('auth/register.html')


@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            name = request.form.get('name')
            email = request.form.get('email')
            password = request.form.get('password')
            phone = request.form.get('phone', '').strip()
            referral_code = (request.form.get('referral_code') or request.args.get('ref') or '').strip()
            
            # Validation
            if not all([name, email, password, phone]):
                flash('All fields are required', 'error')
                return render_template('auth/register.html')
            
            # Validate phone field
            if '@' in phone or len(phone) > 20:
                flash('Invalid phone number. Please enter a valid phone number (not an email).', 'error')
                return render_template('auth/register.html')
            
            if len(phone) < 10:
                flash('Phone number must be at least 10 digits.', 'error')
                return render_template('auth/register.html')
            
            if User.query.filter_by(email=email).first():
                flash('Email already registered', 'error')
                return render_template('auth/register.html')
            
            # Validate referral code if provided
            referred_by_user = None
            if referral_code:
                referred_by_user = User.query.filter_by(referral_code=referral_code).first()
                if not referred_by_user:
                    flash('Invalid referral code', 'error')
                    return render_template('auth/register.html')
            
            # Create user
            user = User(
                name=name,
                email=email,
                password_hash=generate_password_hash(password),
                phone=phone,
                referred_by=referred_by_user.id if referred_by_user else None
            )
            
            db.session.add(user)
            db.session.commit()
            
            # Give welcome bonus to new user
            user.main_balance += SYSTEM_SETTINGS['WELCOME_BONUS']
            
            # Give referral bonus if applicable
            if referred_by_user:
                bonus_amount = 00.0  # ₦500 referral bonus
                referred_by_user.main_balance += bonus_amount
                db.session.commit()
                flash(f'Registration successful! Welcome bonus of ₦{SYSTEM_SETTINGS["WELCOME_BONUS"]:,.0f} added to your account. Please login.', 'success')
            else:
                db.session.commit()
                flash(f'Registration successful! Welcome bonus of ₦{SYSTEM_SETTINGS["WELCOME_BONUS"]:,.0f} added to your account. Please login.', 'success')
            # Prefer explicit public base URL for email links in hosted environments.
            login_url = build_public_url('login')
            send_welcome_email(name, email, login_url)
            return redirect(url_for('login'))
        
        except Exception as e:
            # Log the error and show user-friendly message
            print(f"❌ Registration error: {e}")
            import traceback
            traceback.print_exc()
            db.session.rollback()
            flash('Registration failed. Please try again or contact support.', 'error')
            return render_template('auth/register.html')
    
    return render_template('auth/register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Normalize inputs to avoid autofill/whitespace issues
        raw_email = request.form.get('email') or ''
        raw_password = request.form.get('password') or ''
        email = raw_email.strip()
        password = raw_password.strip()

        # Case-insensitive email lookup (supports mixed-case emails)
        from sqlalchemy import func
        user = User.query.filter(func.lower(User.email) == func.lower(email)).first()
        
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['show_whatsapp_popup'] = True
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password', 'error')
    
    return render_template('auth/login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    flash('Logged out successfully', 'info')
    return redirect(url_for('index'))

@app.route('/dashboard')
@require_login
def dashboard():
    user = get_current_user()
    show_whatsapp_popup = session.pop('show_whatsapp_popup', False)
    whatsapp_channel_url = (os.environ.get('WHATSAPP_CHANNEL_URL') or '').strip()
    
    # Get packages for display
    packages = Package.query.filter_by(is_active=True).all()
    
    # Get featured package (highest ROI or first available)
    featured_package = None
    if packages:
        # Sort by ROI percentage and get the highest one
        featured_package = max(packages, key=lambda p: p.get_roi_percentage())
    
    # Get user's active packages
    user_packages = UserPackage.query.filter_by(user_id=user.id).all()
    active_packages = [up for up in user_packages if up.status == 'active']
    
    # Calculate dashboard statistics
    total_invested = sum(up.amount_invested for up in user_packages)
    total_earned = sum(up.get_earnings_to_date() for up in active_packages)
    daily_income = sum(up.daily_return for up in active_packages)
    daily_cashback = daily_income

    income_scheduler_status = 'No active package yet'
    if active_packages:
        next_payout_times = []
        for user_package in active_packages:
            if user_package.last_payout:
                next_payout_times.append(user_package.last_payout + timedelta(hours=SYSTEM_SETTINGS['INCOME_DROP_HOURS']))
            else:
                next_payout_times.append(user_package.start_date + timedelta(hours=SYSTEM_SETTINGS['INCOME_DROP_HOURS']))

        next_payout_time = min(next_payout_times)
        seconds_remaining = max(0, int((next_payout_time - utc_now()).total_seconds()))
        hours_remaining, remainder = divmod(seconds_remaining, 3600)
        minutes_remaining = remainder // 60

        if seconds_remaining == 0:
            income_scheduler_status = 'Auto payout due now'
        elif hours_remaining > 0:
            income_scheduler_status = f'Auto payout in {hours_remaining}h {minutes_remaining}m'
        else:
            income_scheduler_status = f'Auto payout in {minutes_remaining}m'

    recent_transactions = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.created_at.desc()).limit(5).all()
    
    return render_template('dashboard/mobile_design.html', 
                         user=user, 
                         packages=packages,
                         featured_package=featured_package,
                         user_packages=user_packages,
                         active_packages=active_packages,
                         total_invested=total_invested,
                         total_earned=total_earned,
                         daily_income=daily_income,
                         daily_cashback=daily_cashback,
                         income_scheduler_status=income_scheduler_status,
                         recent_transactions=recent_transactions,
                         show_whatsapp_popup=show_whatsapp_popup,
                         whatsapp_channel_url=whatsapp_channel_url)

@app.route('/packages')
def packages():
    all_packages = Package.query.filter_by(is_active=True).all()
    current_user = get_current_user() if 'user_id' in session else None
    user_packages = []
    if current_user:
        user_packages = UserPackage.query.filter_by(user_id=current_user.id).all()

    return render_template(
        'package/index.html',
        packages=all_packages,
        user_packages=user_packages,
        user=current_user,
        current_user=current_user,
    )

@app.route('/package/<int:package_id>')
def package_details(package_id):
    package = Package.query.get_or_404(package_id)
    user = get_current_user() if 'user_id' in session else None
    return render_template('package/details.html', package=package, user=user)

@app.route('/buy_package/<int:package_id>', methods=['GET', 'POST'])
@require_login
def buy_package(package_id):
    package = Package.query.get_or_404(package_id)
    user = get_current_user()
    purchase_amount = package.price
    daily_income_amount = package.daily_income
    total_return_amount = package.total_return
    
    if request.method == 'POST':
        # Check if this is an AJAX request
        if request.headers.get('Content-Type') == 'application/json' or request.is_json:
            try:
                # Validate package is active
                if not package.is_active:
                    return jsonify({
                        'success': False,
                        'message': '❌ Purchase Failed! This package is not currently available.',
                        'redirect': url_for('packages')
                    })
                # Check if user has sufficient balance
                if user.recharge_balance < purchase_amount:
                    required = purchase_amount - user.recharge_balance
                    return jsonify({
                        'success': False,
                        'message': f'❌ Purchase Failed! Insufficient balance. You need ₦{required:,.0f} more to purchase this package.',
                        'redirect': url_for('deposit')
                    })
                
                # Check if user already has this package active
                existing_package = UserPackage.query.filter_by(
                    user_id=user.id, 
                    package_id=package.id, 
                    is_active=True
                ).filter(UserPackage.end_date > utc_now()).first()
                
                if existing_package:
                    return jsonify({
                        'success': False,
                        'message': f'❌ Purchase Failed! You already have an active {package.name} package.',
                        'redirect': None
                    })
                
                # Deduct from recharge balance
                user.recharge_balance -= purchase_amount
                
                # Create UserPackage record
                from datetime import datetime, timedelta
                user_package = UserPackage(
                    user_id=user.id,
                    package_id=package.id,
                    amount_invested=purchase_amount,
                    start_date=utc_now(),
                    end_date=utc_now() + timedelta(days=package.duration),
                    purchase_date=utc_now(),
                    expiry_date=utc_now() + timedelta(days=package.duration),
                    daily_return=daily_income_amount,
                    is_active=True
                )
                
                # Create transaction record
                transaction = Transaction(
                    user_id=user.id,
                    type='package_purchase',
                    amount=-purchase_amount,  # Negative because it's an expense
                    description=f'Purchased {package.name}',
                    status='completed'
                )
                
                # Save to database and distribute referral commissions
                db.session.add(user_package)
                db.session.add(transaction)
                distribute_referral_commissions(buyer=user, purchase_amount=purchase_amount, source_desc=f"{package.name} package")
                db.session.commit()
                
                # Humanize interval (hours vs minutes)
                interval_hours = SYSTEM_SETTINGS['INCOME_DROP_HOURS']
                interval_text = (
                    f"{int(interval_hours)} hour(s)" if interval_hours >= 1 else f"{int(interval_hours*60)} minute(s)"
                )
                send_package_purchase_email(
                    user_name=user.name,
                    user_email=user.email,
                    package_name=package.name,
                    package_price=purchase_amount,
                    daily_income=daily_income_amount,
                    duration_days=package.duration,
                    total_return=total_return_amount,
                    purchase_date=user_package.purchase_date.strftime('%Y-%m-%d %H:%M UTC'),
                    dashboard_url=build_public_url('dashboard')
                )

                return jsonify({
                    'success': True,
                    'message': f"🎉 Purchase Successful! You have successfully purchased {package.name}. Daily income of ₦{daily_income_amount:,.0f} will start in {interval_text}!",
                    'new_balance': user.recharge_balance,
                    'redirect': url_for('dashboard')
                })
                
            except Exception as e:
                # Rollback transaction on error
                db.session.rollback()
                # Log and surface minimal error detail
                print(f"Error in buy_package (JSON): {e}")
                return jsonify({
                    'success': False,
                    'message': f'❌ Purchase Failed! {str(e)}',
                    'redirect': None
                })
        else:
            # Handle regular form submission (fallback)
            try:
                # Validate package is active
                if not package.is_active:
                    flash('❌ Purchase Failed! This package is not currently available.', 'danger')
                    return redirect(url_for('packages'))
                # Check if user has sufficient balance
                if user.recharge_balance < purchase_amount:
                    required = purchase_amount - user.recharge_balance
                    flash(f'❌ Purchase Failed! Insufficient balance. You need ₦{required:,.0f} more to purchase this package.', 'danger')
                    return redirect(url_for('deposit'))
                
                # Check if user already has this package active
                existing_package = UserPackage.query.filter_by(
                    user_id=user.id, 
                    package_id=package.id, 
                    is_active=True
                ).filter(UserPackage.end_date > utc_now()).first()
                
                if existing_package:
                    flash(f'❌ Purchase Failed! You already have an active {package.name} package.', 'warning')
                    return redirect(url_for('package_details', package_id=package.id))
                
                # Deduct from recharge balance
                user.recharge_balance -= purchase_amount
                
                # Create UserPackage record
                from datetime import datetime, timedelta
                user_package = UserPackage(
                    user_id=user.id,
                    package_id=package.id,
                    amount_invested=purchase_amount,
                    start_date=utc_now(),
                    end_date=utc_now() + timedelta(days=package.duration),
                    purchase_date=utc_now(),
                    expiry_date=utc_now() + timedelta(days=package.duration),
                    daily_return=daily_income_amount,
                    is_active=True
                )
                
                # Create transaction record
                transaction = Transaction(
                    user_id=user.id,
                    type='package_purchase',
                    amount=-purchase_amount,  # Negative because it's an expense
                    description=f'Purchased {package.name}',
                    status='completed'
                )
                
                # Save to database and distribute referral commissions
                db.session.add(user_package)
                db.session.add(transaction)
                distribute_referral_commissions(buyer=user, purchase_amount=purchase_amount, source_desc=f"{package.name} package")
                db.session.commit()
                
                # Reflect the configured drop interval in the success message
                interval_hours = SYSTEM_SETTINGS['INCOME_DROP_HOURS']
                interval_text = (
                    f"{int(interval_hours)} hour(s)" if interval_hours >= 1 else f"{int(interval_hours*60)} minute(s)"
                )
                send_package_purchase_email(
                    user_name=user.name,
                    user_email=user.email,
                    package_name=package.name,
                    package_price=purchase_amount,
                    daily_income=daily_income_amount,
                    duration_days=package.duration,
                    total_return=total_return_amount,
                    purchase_date=user_package.purchase_date.strftime('%Y-%m-%d %H:%M UTC'),
                    dashboard_url=build_public_url('dashboard')
                )
                flash(f"🎉 Purchase Successful! You have successfully purchased {package.name}. Daily income of ₦{daily_income_amount:,.0f} will start in {interval_text}!", 'success')
                return redirect(url_for('dashboard'))
                
            except Exception as e:
                # Rollback transaction on error
                db.session.rollback()
                print(f"Error in buy_package (form): {e}")
                flash(f'❌ Purchase Failed! {str(e)}', 'danger')
                return redirect(url_for('package_details', package_id=package.id))
    
    # For GET, redirect to the package details page where purchase is handled
    return redirect(url_for('package_details', package_id=package.id))

@app.route('/deposit')
@require_login
def deposit():
    user = get_current_user()
    # Refresh SYSTEM_SETTINGS from DB
    global SYSTEM_SETTINGS
    SYSTEM_SETTINGS = load_system_settings()

    deposit_gateway_mode = (SYSTEM_SETTINGS.get('DEPOSIT_GATEWAY_MODE') or 'automatic').lower()
    if deposit_gateway_mode == 'manual':
        return deposit_manual()
    if deposit_gateway_mode == 'automatic':
        return deposit_automatic()

    return render_template('deposit/index.html', user=user)


@app.route('/deposit/manual')
@require_login
def deposit_manual():
    user = get_current_user()

    # Get bank details via execute_one
    bank_row = execute_one('SELECT * FROM bank_details WHERE is_active = TRUE LIMIT 1')

    # Convert to object-like for template
    class DictObj:
        def __init__(self, d):
            if not d:
                d = {}
            self.__dict__.update(d)

    if not bank_row:
        bank_details = DictObj({
            'bank_name': '',
            'account_number': '',
            'account_name': ''
        })
    else:
        bank_details = DictObj(bank_row)

    global SYSTEM_SETTINGS
    SYSTEM_SETTINGS = load_system_settings()

    deposit_gateway_mode = (SYSTEM_SETTINGS.get('DEPOSIT_GATEWAY_MODE') or 'automatic').lower()
    if deposit_gateway_mode != 'manual':
        return redirect(url_for('deposit'))

    return render_template('deposit/manual.html',
                         user=user,
                         minimum_deposit=SYSTEM_SETTINGS['MINIMUM_DEPOSIT'],
                         bank_details=bank_details)


@app.route('/deposit/automatic')
@require_login
def deposit_automatic():
    user = get_current_user()
    global SYSTEM_SETTINGS
    SYSTEM_SETTINGS = load_system_settings()

    deposit_gateway_mode = (SYSTEM_SETTINGS.get('DEPOSIT_GATEWAY_MODE') or 'automatic').lower()
    if deposit_gateway_mode != 'automatic':
        return redirect(url_for('deposit'))

    provider_min = float(getattr(gtr_pay_service, 'min_amount', 0.0) or 0.0)
    provider_max = float(getattr(gtr_pay_service, 'max_amount', 0.0) or 0.0)
    effective_min = max(float(SYSTEM_SETTINGS['MINIMUM_DEPOSIT']), provider_min)

    return render_template(
        'deposit/gtr.html',
        user=user,
        minimum_deposit=effective_min,
        maximum_deposit=provider_max,
    )

@app.route('/deposit_history')
@require_login
def deposit_history():
    """Display user's deposit history"""
    user = get_current_user()
    
    # Get all deposit transactions for the user
    deposits = Transaction.query.filter_by(
        user_id=user.id,
        type='deposit'
    ).order_by(Transaction.created_at.desc()).all()
    
    # Calculate deposit statistics
    total_deposits = sum(d.amount for d in deposits if d.status == 'completed')
    pending_deposits = sum(d.amount for d in deposits if d.status == 'pending')
    successful_deposits = len([d for d in deposits if d.status == 'completed'])
    
    return render_template('dashboard/deposit_history.html', 
                         user=user,
                         deposits=deposits,
                         total_deposits=total_deposits,
                         pending_deposits=pending_deposits,
                         successful_deposits=successful_deposits)

@app.route('/withdrawal_history')
@require_login
def withdrawal_history():
    """Display user's withdrawal history"""
    user = get_current_user()
    
    # Get all withdrawals for the user from Withdrawal table
    withdrawals = Withdrawal.query.filter_by(
        user_id=user.id
    ).order_by(Withdrawal.created_at.desc()).all()

    successful_statuses = {'completed', 'approved', 'processed'}
    
    # Calculate withdrawal statistics using the actual fee from database
    total_withdrawals = sum(w.amount for w in withdrawals if w.status in successful_statuses)
    pending_withdrawals = sum(w.amount for w in withdrawals if w.status == 'pending')
    successful_withdrawals = len([w for w in withdrawals if w.status in successful_statuses])
    
    # Calculate total fees paid (use actual fees from database)
    total_fees_paid = sum(w.fee for w in withdrawals if w.status in successful_statuses and w.fee)
    
    # Get current withdrawal fee rate from system settings
    withdrawal_fee_rate = SYSTEM_SETTINGS['WITHDRAWAL_FEE_PERCENTAGE']
    
    return render_template('dashboard/withdrawal_history.html', 
                         user=user,
                         withdrawals=withdrawals,
                         total_withdrawals=total_withdrawals,
                         pending_withdrawals=pending_withdrawals,
                         successful_withdrawals=successful_withdrawals,
                         total_fees_paid=total_fees_paid,
                         withdrawal_fee_rate=withdrawal_fee_rate)


def _withdrawal_access_state(user):
    """Return whether the user can access withdrawals and why not."""
    active_package = UserPackage.query.filter(
        UserPackage.user_id == user.id,
        UserPackage.is_active == True,
        UserPackage.end_date > utc_now()
    ).first()

    has_bank_details = all([
        (user.bank_name or '').strip(),
        (user.account_number or '').strip(),
        (user.account_name or '').strip(),
    ])

    return active_package, has_bank_details


def _deny_withdrawal_access(active_package, has_bank_details, *, json_mode=False):
    """Block withdrawal access until prerequisites are met."""
    if active_package and has_bank_details:
        return None

    if not active_package and not has_bank_details:
        message = 'Add your bank details and buy an active package before you can access withdrawals.'
        if json_mode:
            return jsonify({'success': False, 'requires_bank_details': True, 'requires_package': True, 'message': message})
        flash(message, 'error')
        return redirect(url_for('dashboard'))

    if not has_bank_details:
        message = 'Add your bank details before you can access withdrawals.'
        if json_mode:
            return jsonify({'success': False, 'requires_bank_details': True, 'message': message})
        flash(message, 'error')
        return redirect(url_for('bank_details'))

    message = 'Buy an active package before you can access withdrawals.'
    if json_mode:
        return jsonify({'success': False, 'requires_package': True, 'message': message})
    flash(message, 'error')
    return redirect(url_for('packages'))

@app.route('/withdrawal')
@require_login
def withdrawal():
    user = get_current_user()
    global SYSTEM_SETTINGS
    SYSTEM_SETTINGS = load_system_settings()
    active_pkg, has_bank_details = _withdrawal_access_state(user)
    access_block = _deny_withdrawal_access(active_pkg, has_bank_details)
    if access_block:
        return access_block
    require_package = False
    
    # Use system settings for withdrawal parameters
    minimum_withdrawal = SYSTEM_SETTINGS['MINIMUM_WITHDRAWAL']
    withdrawal_fee_percentage = SYSTEM_SETTINGS['WITHDRAWAL_FEE_PERCENTAGE']
    processing_time = "0-2 hrs"
    withdrawal_hours = format_withdrawal_window(
        SYSTEM_SETTINGS['WITHDRAWAL_START_TIME'],
        SYSTEM_SETTINGS['WITHDRAWAL_END_TIME']
    )
    withdrawal_locked = not is_withdrawal_window_open(
        SYSTEM_SETTINGS['WITHDRAWAL_START_TIME'],
        SYSTEM_SETTINGS['WITHDRAWAL_END_TIME']
    )
    
    # Get user's pending withdrawals from Withdrawal table
    pending_withdrawals = Withdrawal.query.filter_by(
        user_id=user.id,
        status='pending'
    ).all()
    
    # Get all user withdrawals for history
    all_withdrawals = Withdrawal.query.filter_by(
        user_id=user.id
    ).order_by(Withdrawal.created_at.desc()).all()
    
    # Calculate total pending amount
    total_pending = sum(w.amount for w in pending_withdrawals)
    
    # Calculate available balance (main balance minus pending withdrawals)
    available_balance = max(0, user.main_balance - total_pending)
    
    return render_template('withdrawal/index.html', 
                         user=user,
                         minimum_withdrawal=minimum_withdrawal,
                         withdrawal_fee_percentage=withdrawal_fee_percentage,
                         processing_time=processing_time,
                         withdrawal_hours=withdrawal_hours,
                         withdrawal_locked=withdrawal_locked,
                         pending_withdrawals=pending_withdrawals,
                         withdrawals=all_withdrawals,
                         total_pending=total_pending,
                         available_balance=available_balance,
                         require_package=require_package)

@app.route('/request_withdrawal')
@require_login
def request_withdrawal():
    user = get_current_user()
    global SYSTEM_SETTINGS
    SYSTEM_SETTINGS = load_system_settings()
    active_pkg, has_bank_details = _withdrawal_access_state(user)
    access_block = _deny_withdrawal_access(active_pkg, has_bank_details)
    if access_block:
        return access_block
    require_package = False
    
    # Use system settings for withdrawal parameters
    minimum_withdrawal = SYSTEM_SETTINGS['MINIMUM_WITHDRAWAL']
    withdrawal_fee_percentage = SYSTEM_SETTINGS['WITHDRAWAL_FEE_PERCENTAGE']
    withdrawal_hours = format_withdrawal_window(
        SYSTEM_SETTINGS['WITHDRAWAL_START_TIME'],
        SYSTEM_SETTINGS['WITHDRAWAL_END_TIME']
    )
    withdrawal_locked = not is_withdrawal_window_open(
        SYSTEM_SETTINGS['WITHDRAWAL_START_TIME'],
        SYSTEM_SETTINGS['WITHDRAWAL_END_TIME']
    )
    
    return render_template('withdrawal/request.html', 
                         current_user=user,
                         SYSTEM_SETTINGS=SYSTEM_SETTINGS,
                         require_package=require_package,
                         processing_time="0-2 hrs",
                         withdrawal_hours=withdrawal_hours,
                         withdrawal_locked=withdrawal_locked)

@app.route('/transactions')
@require_login
def transactions():
    """Display user's income and transaction records"""
    user = get_current_user()
    
    # Get all transactions for the user, ordered by most recent
    user_transactions = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.created_at.desc()).all()

    successful_withdrawal_statuses = {'completed', 'approved', 'processed'}

    # Confirm withdrawal transaction status from the source withdrawal record when possible.
    # This keeps the history page aligned with the admin-reviewed withdrawal status.
    for transaction in user_transactions:
        if transaction.type != 'withdrawal':
            continue

        linked_withdrawal = None

        if transaction.reference:
            reference_match = re.match(r'^WD-(\d+)$', transaction.reference)
            if reference_match:
                linked_withdrawal = Withdrawal.query.filter_by(
                    id=int(reference_match.group(1)),
                    user_id=user.id
                ).first()

        if not linked_withdrawal and transaction.amount < 0:
            withdrawal_amount = abs(transaction.amount)
            matching_withdrawals = [
                withdrawal for withdrawal in user.withdrawals
                if abs(withdrawal.amount - withdrawal_amount) < 0.01
            ]

            if len(matching_withdrawals) == 1:
                linked_withdrawal = matching_withdrawals[0]
            elif matching_withdrawals:
                matching_withdrawals.sort(
                    key=lambda withdrawal: abs((withdrawal.created_at - transaction.created_at).total_seconds())
                    if withdrawal.created_at and transaction.created_at else 0
                )
                closest_withdrawal = matching_withdrawals[0]
                if closest_withdrawal.created_at and transaction.created_at:
                    delta_seconds = abs((closest_withdrawal.created_at - transaction.created_at).total_seconds())
                    if delta_seconds <= 3600:
                        linked_withdrawal = closest_withdrawal

        if linked_withdrawal:
            if linked_withdrawal.status in successful_withdrawal_statuses:
                transaction.status = 'completed'
            elif linked_withdrawal.status in ['rejected', 'cancelled']:
                transaction.status = 'failed'
            elif linked_withdrawal.status == 'pending':
                transaction.status = 'pending'
    
    # Separate transactions by type for better display
    income_transactions = [t for t in user_transactions if t.type in ['package_income', 'daily_bonus', 'referral_bonus']]
    other_transactions = [t for t in user_transactions if t.type not in ['package_income', 'daily_bonus', 'referral_bonus']]
    
    # Calculate income statistics
    total_package_income = sum(t.amount for t in user_transactions if t.type == 'package_income')
    total_daily_bonus = sum(t.amount for t in user_transactions if t.type == 'daily_bonus')
    total_referral_bonus = sum(t.amount for t in user_transactions if t.type == 'referral_bonus')
    
    # Get active packages for income tracking with enhanced data
    active_packages = UserPackage.query.filter(
        UserPackage.user_id == user.id,
        UserPackage.is_active == True,
        UserPackage.end_date > utc_now()
    ).all()
    
    # Add next income time calculation for each package
    for package in active_packages:
        # Calculate next income time based on INCOME_DROP_HOURS
        if package.last_payout:
            next_income = package.last_payout + timedelta(hours=SYSTEM_SETTINGS['INCOME_DROP_HOURS'])
        else:
            # If no previous income, schedule first income based on configured interval
            next_income = utc_now() + timedelta(hours=SYSTEM_SETTINGS['INCOME_DROP_HOURS'])
        
        package.next_income_time = next_income.isoformat()
    
    return render_template('transaction/income_record.html', 
                         user=user,
                         user_transactions=user_transactions,
                         transactions=user_transactions,
                         income_transactions=income_transactions,
                         other_transactions=other_transactions,
                         total_package_income=total_package_income,
                         total_daily_bonus=total_daily_bonus,
                         total_referral_bonus=total_referral_bonus,
                         active_packages=active_packages,
                         now=utc_now)

@app.route('/test-referral-page')
def test_referral_page():
    """Test route for referral template without authentication"""
    print("🔍 DEBUG: Test referral route accessed")
    
    # Create mock user data
    class MockUser:
        def __init__(self):
            self.id = 1
            self.username = 'testuser'
            self.referral_code = 'TEST123'
            self.account_balance = 5000.0
            self.email = 'test@example.com'
    
    user = MockUser()
    
    # Mock template data
    template_data = {
        'user': user,
        'level1_earnings': 1000.0,
        'level2_earnings': 500.0,
        'level3_earnings': 200.0,
        'level1_commission': 240.0,
        'level2_commission': 20.0,
        'level3_commission': 4.0,
        'level1_referrals': [],
        'level2_referrals': [],
        'level3_referrals': [],
        'total_referrals': 3,
        'active_referrals': 2,
        'total_commission': 264.0
    }
    
    try:
        print("🔍 DEBUG: Rendering simple referral template...")
        return render_template('referral/index.html', **template_data)
    except Exception as e:
        print(f"❌ DEBUG: Template error: {e}")
        return f"Template Error: {str(e)}"

@app.route('/referral')
@require_login
def referral():
    """Team Work / Referral page - shows beautiful blue Team Work interface"""
    user = get_current_user()
    
    if not user:
        flash('Please log in to view your referrals', 'error')
        return redirect(url_for('login'))
    
    try:
        global SYSTEM_SETTINGS
        SYSTEM_SETTINGS = load_system_settings()
        referral_rates = SYSTEM_SETTINGS.get('REFERRAL_COMMISSIONS', {})
        level1_rate = float(referral_rates.get('LEVEL_1', 0.15))
        level2_rate = float(referral_rates.get('LEVEL_2', 0.03))
        level3_rate = float(referral_rates.get('LEVEL_3', 0.01))

        # Get level 1 referrals (direct referrals)
        level1_referrals = User.query.filter_by(referred_by=user.id).all()
        
        # Get level 2 referrals (referrals of level 1 referrals)
        level2_referrals = []
        for ref in level1_referrals:
            level2_referrals.extend(User.query.filter_by(referred_by=ref.id).all())
        
        # Get level 3 referrals (referrals of level 2 referrals)
        level3_referrals = []
        for ref in level2_referrals:
            level3_referrals.extend(User.query.filter_by(referred_by=ref.id).all())
        
        # Calculate actual earned referral commissions credited to the current user.
        def sum_referral_bonus(level_number=None):
            query = db.session.query(db.func.coalesce(db.func.sum(Transaction.amount), 0.0)).filter(
                Transaction.user_id == user.id,
                Transaction.type == 'referral_bonus',
                Transaction.status == 'completed'
            )

            if level_number is not None:
                query = query.filter(Transaction.description.contains(f'Level {level_number} referral commission'))

            total = query.scalar() or 0.0
            return float(total)

        level1_commission = sum_referral_bonus(1)
        level2_commission = sum_referral_bonus(2)
        level3_commission = sum_referral_bonus(3)

        total_commission = sum_referral_bonus()

        # Keep these for template compatibility and future expansion.
        level1_earnings = level1_commission
        level2_earnings = level2_commission
        level3_earnings = level3_commission
        total_referrals = len(level1_referrals) + len(level2_referrals) + len(level3_referrals)
        active_referrals = len([ref for ref in level1_referrals + level2_referrals + level3_referrals if ref.is_active])
        
        # Template data for Team Work page
        template_data = {
            'user': user,
            'current_user': user,
            'level1_referrals': level1_referrals,
            'level2_referrals': level2_referrals,
            'level3_referrals': level3_referrals,
            'level1_earnings': level1_earnings,
            'level2_earnings': level2_earnings,
            'level3_earnings': level3_earnings,
            'level1_commission': level1_commission,
            'level2_commission': level2_commission,
            'level3_commission': level3_commission,
            'level1_rate_pct': round(level1_rate * 100, 2),
            'level2_rate_pct': round(level2_rate * 100, 2),
            'level3_rate_pct': round(level3_rate * 100, 2),
            'total_referrals': total_referrals,
            'active_referrals': active_referrals,
            'total_commission': total_commission
        }
        
        # Render the beautiful blue Team Work template
        return render_template('referral/team_work.html', **template_data)
        
    except Exception as e:
        flash(f'Error loading team work page: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@app.route('/invite-friends')
@require_login
def invite_friends():
    """Invite Friends page - shows QR code and referral link"""
    user = get_current_user()
    
    if not user:
        flash('Please log in to view your invite page', 'error')
        return redirect(url_for('login'))
    
    try:
        # Get referral statistics (same as referral page)
        level1_referrals = User.query.filter_by(referred_by=user.id).all()
        level2_referrals = []
        level3_referrals = []
        
        # Get second level referrals
        for ref in level1_referrals:
            level2_referrals.extend(User.query.filter_by(referred_by=ref.id).all())
        
        # Get third level referrals
        for ref in level2_referrals:
            level3_referrals.extend(User.query.filter_by(referred_by=ref.id).all())
        
        return render_template('referral/invite_friends.html',
                             user=user,
                             current_user=user,
                             level1_referrals=level1_referrals,
                             level2_referrals=level2_referrals,
                             level3_referrals=level3_referrals)
        
    except Exception as e:
        flash(f'Error loading invite friends page: {str(e)}', 'error')
        return redirect(url_for('referral'))

@app.route('/profile')
@require_login
def profile():
    user = get_current_user()
    # Include recent withdrawals for the profile summary (last 5)
    recent_withdrawals = Withdrawal.query.filter_by(user_id=user.id).order_by(Withdrawal.created_at.desc()).limit(5).all()
    return render_template('dashboard/profile.html', user=user, recent_withdrawals=recent_withdrawals)


def build_package_details(user_packages):
    """Helper to construct package detail dicts for templates."""
    package_details = []
    for user_package in user_packages:
        days_since_purchase = (utc_now() - user_package.start_date).days
        remaining_days = max(0, (user_package.end_date - utc_now()).days)
        total_income = user_package.total_earned or 0.0
        daily_return = user_package.amount_invested * (user_package.package.daily_return_rate / 100) if getattr(user_package.package, 'daily_return_rate', None) is not None else user_package.daily_return
        total_duration = (user_package.end_date - user_package.start_date).days if user_package.end_date and user_package.start_date else 0
        if total_duration > 0:
            progress_percentage = min(100, (days_since_purchase / total_duration) * 100)
        else:
            progress_percentage = 0

        package_details.append({
            'user_package': user_package,
            'package': user_package.package,
            'remaining_days': remaining_days,
            'days_since_purchase': days_since_purchase,
            'daily_return': daily_return,
            'total_income': total_income,
            'progress_percentage': progress_percentage,
            'is_completed': remaining_days == 0
        })

    return package_details

@app.route('/my-packages')
@require_login
def my_packages():
    """Show user's active packages with remaining days and income"""
    user = get_current_user()
    # Get user's active packages
    user_packages = UserPackage.query.filter_by(user_id=user.id).all()

    # Filter for only active packages
    active_packages = [up for up in user_packages if up.status == 'active']
    package_details = build_package_details(active_packages)

    return render_template('package/my_packages.html', 
                         current_user=user,
                         package_details=package_details)


@app.route('/my-packages/active')
@require_login
def my_packages_active():
    user = get_current_user()
    user_packages = UserPackage.query.filter_by(user_id=user.id).all()
    active_packages = [up for up in user_packages if up.status == 'active']
    package_details = build_package_details(active_packages)
    return render_template('package/my_packages.html', current_user=user, package_details=package_details)


@app.route('/my-packages/completed')
@require_login
def my_packages_completed():
    user = get_current_user()
    user_packages = UserPackage.query.filter_by(user_id=user.id).all()
    completed_packages = [up for up in user_packages if up.status == 'completed']
    package_details = build_package_details(completed_packages)
    return render_template('package/my_packages.html', current_user=user, package_details=package_details)


@app.route('/my-packages/expired')
@require_login
def my_packages_expired():
    user = get_current_user()
    user_packages = UserPackage.query.filter_by(user_id=user.id).all()
    expired_packages = [up for up in user_packages if up.status == 'expired']
    package_details = build_package_details(expired_packages)
    return render_template('package/my_packages.html', current_user=user, package_details=package_details)

@app.route('/bank-details')
@require_login
def bank_details():
    """Bank details management page"""
    user = get_current_user()
    return render_template('bank/bank_details.html', current_user=user)

@app.route('/update-bank-details', methods=['POST'])
@require_login
def update_bank_details():
    """Update user's bank details"""
    user = get_current_user()
    
    try:
        # Handle both JSON and form data
        if request.content_type == 'application/json':
            data = request.get_json()
            bank_name = data.get('bank_name', '').strip()
            account_number = data.get('account_number', '').strip()
            account_name = data.get('account_name', '').strip()
            action = data.get('action', '')
        else:
            bank_name = request.form.get('bank_name', '').strip()
            account_number = request.form.get('account_number', '').strip()
            account_name = request.form.get('account_name', '').strip()
            action = request.form.get('action', '')
        
        # Handle delete action
        if action == 'delete':
            user.bank_name = None
            user.account_number = None
            user.account_name = None
            db.session.commit()
            return jsonify({'success': True, 'message': 'Bank details removed successfully'})
        
        # Validation
        if not bank_name:
            return jsonify({'success': False, 'message': 'Bank name is required'})
        
        if not account_number:
            return jsonify({'success': False, 'message': 'Account number is required'})
            
        if not account_name:
            return jsonify({'success': False, 'message': 'Account name is required'})
        
        # Validate account number (should be digits only and reasonable length)
        if not account_number.isdigit():
            return jsonify({'success': False, 'message': 'Account number should contain only digits'})
        
        if len(account_number) < 10 or len(account_number) > 20:
            return jsonify({'success': False, 'message': 'Account number should be between 10-20 digits'})
        
        # Update user's bank details
        user.bank_name = bank_name
        user.account_number = account_number
        user.account_name = account_name
        
        db.session.commit()
        
        # Log the bank details update
        bank_update_transaction = Transaction(
            user_id=user.id,
            type='profile_update',
            amount=0.0,
            description=f'Bank details updated: {bank_name} - {account_number}',
            status='completed'
        )
        db.session.add(bank_update_transaction)
        db.session.commit()
        
        return jsonify({
            'success': True, 
            'message': 'Bank details updated successfully'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False, 
            'message': f'An error occurred: {str(e)}'
        })

@app.route('/security')
@require_login
def security_manager():
    """Security manager page for password reset and account security"""
    user = get_current_user()
    return render_template('security/manager.html', user=user)

@app.route('/change_password', methods=['POST'])
@require_login
def change_password():
    """Handle password change requests"""
    user = get_current_user()
    
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')
    
    # Validation
    if not all([current_password, new_password, confirm_password]):
        flash('All fields are required', 'error')
        return redirect(url_for('security_manager'))
    
    # Verify current password
    if not check_password_hash(user.password_hash, current_password):
        flash('Current password is incorrect', 'error')
        return redirect(url_for('security_manager'))
    
    # Check if new passwords match
    if new_password != confirm_password:
        flash('New password and confirmation do not match', 'error')
        return redirect(url_for('security_manager'))
    
    # Password strength validation
    if len(new_password) < 8:
        flash('Password must be at least 8 characters long', 'error')
        return redirect(url_for('security_manager'))
    
    # Check for password complexity
    if not re.search(r'[A-Z]', new_password):
        flash('Password must contain at least one uppercase letter', 'error')
        return redirect(url_for('security_manager'))
    
    if not re.search(r'[a-z]', new_password):
        flash('Password must contain at least one lowercase letter', 'error')
        return redirect(url_for('security_manager'))
    
    if not re.search(r'\d', new_password):
        flash('Password must contain at least one number', 'error')
        return redirect(url_for('security_manager'))
    
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', new_password):
        flash('Password must contain at least one special character', 'error')
        return redirect(url_for('security_manager'))
    
    # Check if new password is different from current
    if check_password_hash(user.password_hash, new_password):
        flash('New password must be different from current password', 'error')
        return redirect(url_for('security_manager'))
    
    try:
        # Update password
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        
        # Log the password change (for security tracking)
        password_change_transaction = Transaction(
            user_id=user.id,
            type='security_update',
            amount=0.0,
            description='Password changed successfully',
            status='completed'
        )
        db.session.add(password_change_transaction)
        db.session.commit()
        
        flash('Password updated successfully! Please login again for security.', 'success')
        
        # For security, log out user after password change
        session.clear()
        return redirect(url_for('login'))
        
    except Exception as e:
        flash('An error occurred while updating password. Please try again.', 'error')
        return redirect(url_for('security_manager'))

@app.route('/claim_bonus', methods=['POST'])
@require_login
def claim_bonus():
    """Handle daily check-in bonus claims"""
    user = get_current_user()
    
    try:
        # Check if user has already claimed today's bonus
        today = utc_now().date()
        if user.last_checkin and user.last_checkin.date() == today:
            return jsonify({
                'success': False,
                'message': 'You have already claimed your daily bonus today! Come back tomorrow.'
            })
        
        # Award the daily check-in bonus
        bonus_amount = SYSTEM_SETTINGS['DAILY_CHECKIN_BONUS']
        user.main_balance += bonus_amount
        user.last_checkin = utc_now()
        
        # Create a transaction record for the bonus
        transaction = Transaction(
            user_id=user.id,
            type='daily_bonus',
            amount=bonus_amount,
            description=f'Daily Check-in Bonus',
            status='completed'
        )
        
        db.session.add(transaction)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Daily bonus of ₦{bonus_amount:,.0f} claimed successfully!',
            'bonus_amount': bonus_amount,
            'new_balance': user.main_balance
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'message': f'An error occurred while claiming your bonus: {str(e)}'
        }), 400

@app.route('/process_deposit', methods=['POST'])
@require_login  
def process_deposit():
    """Handle GTR Pay deposit processing"""
    user = get_current_user()
    global SYSTEM_SETTINGS
    SYSTEM_SETTINGS = load_system_settings()
    
    amount = request.form.get('amount')
    payment_method = request.form.get('payment_method', 'gtr_pay')
    
    if not amount:
        return jsonify({'success': False, 'message': 'Amount is required'})
    
    try:
        amount = float(amount)
        provider_min = float(getattr(gtr_pay_service, 'min_amount', 0.0) or 0.0)
        provider_max = float(getattr(gtr_pay_service, 'max_amount', 0.0) or 0.0)
        effective_min = max(float(SYSTEM_SETTINGS['MINIMUM_DEPOSIT']), provider_min)

        if amount < effective_min:
            return jsonify({'success': False, 'message': f'Minimum automatic deposit amount is ₦{effective_min:,.0f}'})

        if provider_max and amount > provider_max:
            return jsonify({'success': False, 'message': f'Maximum automatic deposit amount is ₦{provider_max:,.0f}'})
        
        # Generate unique reference with timestamp
        import secrets
        from datetime import datetime
        timestamp = datetime.now().strftime("%m%d%H%M")
        reference = f"GTR{timestamp}{secrets.token_urlsafe(4)[:4].upper()}"
        
        # Create pending transaction record first
        pending_transaction = Transaction(
            user_id=user.id,
            type='deposit',
            amount=amount,
            description=f'Deposit via GTR Pay',
            status='pending',
            reference=reference
        )
        
        db.session.add(pending_transaction)
        db.session.commit()
        
        # Create payment with the documented NekPayment collection API
        callback_url = build_public_url('gtr_payment_callback')
        return_url = build_public_url('payment_return')
        public_base_url = os.environ.get('APP_BASE_URL', '').strip()
        if public_base_url and any(host in callback_url or host in return_url for host in ('127.0.0.1', 'localhost')):
            db.session.delete(pending_transaction)
            db.session.commit()
            print(f"⚠️ Blocking gateway request because APP_BASE_URL is public but callbacks are local: callback={callback_url}, return={return_url}")
            return jsonify({
                'success': False,
                'message': 'Set APP_BASE_URL to your public domain before creating payments. Gateway callback URLs cannot use localhost.',
            })
        if any(host in callback_url or host in return_url for host in ('127.0.0.1', 'localhost')):
            print('⚠️ Using localhost callback URLs for local testing; gateway request will still be sent.')

        print(f"🔄 Creating deposit payment: reference={reference}, amount={amount}, callback={callback_url}, return={return_url}")

        payment_result = gtr_pay_service.create_deposit_payment(
            amount=amount,
            reference=reference,
            callback_url=callback_url,
            page_url=return_url,
            mch_return_msg=reference,
            goods_name='SONICVEST Deposit',
        )

        print(f"📦 Deposit gateway result for {reference}: {payment_result}")
        
        if payment_result['success']:
            # Return payment URL for redirect
            return jsonify({
                'success': True,
                'payment_url': payment_result['payment_url'],
                'reference': reference,
                'trade_no': payment_result['trade_no'],
                'trade_result': payment_result.get('trade_result'),
                'message': 'Payment created successfully. Redirecting to GTR Pay...',
                'raw_response': payment_result.get('raw_response'),
            })
        else:
            # Clean up pending transaction
            db.session.delete(pending_transaction)
            db.session.commit()
            
            return jsonify({
                'success': False,
                'message': f'Payment creation failed: {payment_result["message"]}',
                'raw_response': payment_result.get('raw_response'),
            })
        
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid amount format'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Deposit failed: {str(e)}'})

@app.route('/gtr-payment-callback', methods=['POST'])
def gtr_payment_callback():
    """Handle NekPayment IPN (Instant Payment Notification) callback"""
    try:
        # Get callback data
        if request.is_json:
            callback_data = request.get_json()
        else:
            callback_data = request.form.to_dict()
        
        # Verify callback
        verification_result = gtr_pay_service.verify_payment_callback(callback_data)
        
        if verification_result['success']:
            # Find the pending transaction
            reference = verification_result['reference']
            transaction = Transaction.query.filter_by(reference=reference).first()
            
            if transaction:
                if transaction.status != 'completed':
                    # Update transaction status
                    transaction.status = 'completed'

                    # Add amount to user's recharge balance
                    user = User.query.get(transaction.user_id)
                    user.recharge_balance += transaction.amount

                    db.session.commit()

                    # Log successful payment
                    print(f"✅ NekPayment deposit confirmed: {reference} - ₦{transaction.amount}")

                return "success", 200  # Provider expects lowercase success response
            else:
                print(f"⚠️ Transaction not found: {reference}")
                return "TRANSACTION_NOT_FOUND", 404
        else:
            print(f"❌ NekPayment callback verification failed: {verification_result['message']}")
            return "VERIFICATION_FAILED", 400
            
    except Exception as e:
        print(f"❌ Error processing NekPayment callback: {str(e)}")
        return "ERROR", 500

@app.route('/deposit/receipt/<reference>')
@require_login
def deposit_receipt(reference):
    """Display deposit receipt"""
    user = get_current_user()
    
    # Find the transaction
    transaction = Transaction.query.filter_by(
        reference=reference,
        user_id=user.id,
        type='deposit'
    ).first()
    
    if not transaction:
        flash('Receipt not found or access denied', 'error')
        return redirect(url_for('dashboard'))
    
    # Get additional data for receipt
    from datetime import datetime
    
    return render_template('deposit/receipt.html',
                         transaction=transaction,
                         user=user,
                         trade_no=request.args.get('trade_no'),
                         business_name="Max Wealth",
                         current_time=datetime.now())


@app.route('/payment/status/<reference>')
@require_login
def payment_status(reference):
    """Check whether a deposit has been confirmed by the gateway callback."""
    user = get_current_user()
    transaction = Transaction.query.filter_by(
        reference=reference,
        user_id=user.id,
        type='deposit'
    ).first()

    if not transaction:
        return jsonify({'success': False, 'message': 'Transaction not found'}), 404

    return jsonify({
        'success': True,
        'reference': reference,
        'status': transaction.status,
        'completed': transaction.status == 'completed',
        'retry_after': 3 if transaction.status != 'completed' else 0,
        'redirect_url': url_for('dashboard') if transaction.status == 'completed' else None,
    })


@app.route('/payment/confirm/<reference>', methods=['POST'])
@require_login
def payment_confirm(reference):
    """Ask NekPayment to verify a deposit before crediting the account."""
    user = get_current_user()
    transaction = Transaction.query.filter_by(
        reference=reference,
        user_id=user.id,
        type='deposit'
    ).first()

    if not transaction:
        return jsonify({'success': False, 'completed': False, 'message': 'Transaction not found'}), 404

    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    trade_no = payload.get('trade_no') or payload.get('tradeNo') or request.args.get('trade_no') or request.args.get('tradeNo')

    if transaction.status == 'completed':
        return jsonify({
            'success': True,
            'completed': True,
            'reference': reference,
            'status': 'completed',
            'message': 'Payment already confirmed',
            'redirect_url': url_for('deposit_receipt', reference=reference, trade_no=trade_no or f'GTR{reference}', stay=1),
        })

    verification = gtr_pay_service.verify_deposit_payment(
        reference=reference,
        trade_no=trade_no,
        amount=transaction.amount,
    )

    if verification.get('success') and verification.get('verified'):
        transaction.status = 'completed'
        user.recharge_balance += transaction.amount
        db.session.commit()

        print(f"✅ NekPayment deposit verified on confirm: {reference} - ₦{transaction.amount}")

        return jsonify({
            'success': True,
            'completed': True,
            'reference': reference,
            'status': 'completed',
            'message': verification.get('message') or 'Payment confirmed successfully',
            'gateway_response': verification.get('raw_response'),
            'redirect_url': url_for('deposit_receipt', reference=reference, trade_no=trade_no or f'GTR{reference}', stay=1),
        })

    return jsonify({
        'success': False,
        'completed': False,
        'reference': reference,
        'status': transaction.status,
        'message': verification.get('message') or 'Payment is still pending confirmation',
        'gateway_response': verification.get('raw_response'),
        'redirect_url': None,
    })

@app.route('/process_manual_deposit', methods=['POST'])
@require_login
def process_manual_deposit():
    """Handle manual deposit submission with receipt upload"""
    from werkzeug.utils import secure_filename
    import os
    
    user = get_current_user()
    
    try:
        # Get form data
        receipt_file = request.files.get('receipt')
        
        if not receipt_file:
            return jsonify({'success': False, 'message': 'Payment receipt is required'})
        
        # Validate file
        allowed_extensions = {'png', 'jpg', 'jpeg', 'pdf'}
        filename = secure_filename(receipt_file.filename)
        if '.' not in filename or filename.rsplit('.', 1)[1].lower() not in allowed_extensions:
            return jsonify({'success': False, 'message': 'Invalid file type. Use PNG, JPG, or PDF'})
        
        # Create uploads directory if it doesn't exist
        upload_dir = os.path.join(app.root_path, 'static', 'uploads', 'receipts')
        os.makedirs(upload_dir, exist_ok=True)
        
        # Generate unique filename
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        file_extension = filename.rsplit('.', 1)[1].lower()
        new_filename = f"receipt_{user.id}_{timestamp}.{file_extension}"
        file_path = os.path.join(upload_dir, new_filename)
        
        # Save file
        receipt_file.save(file_path)
        
        # Generate reference
        reference = f"DEP{timestamp}{secrets.token_urlsafe(4)[:4].upper()}"
        
        # Create pending transaction with 0 amount (admin will set actual amount)
        transaction = Transaction(
            user_id=user.id,
            type='deposit',
            amount=0,
            description=f'Manual deposit - Receipt: {new_filename}',
            status='pending',
            reference=reference
        )
        
        db.session.add(transaction)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Deposit request submitted successfully! Waiting for admin approval.',
            'reference': reference
        })
        
    except Exception as e:
        print(f"Error processing manual deposit: {str(e)}")
        return jsonify({'success': False, 'message': 'An error occurred. Please try again.'})

@app.route('/payment/return')
@app.route('/payment/success')
@require_login
def payment_return():
    """Handle the gateway return and move the user back into the app."""
    reference = (
        request.args.get('mchOrderNo')
        or request.args.get('mch_order_no')
        or request.args.get('reference')
        or request.args.get('orderNo')
    )
    trade_no = request.args.get('tradeNo') or request.args.get('trade_no') or request.args.get('orderNo')
    trade_result = request.args.get('tradeResult') or request.args.get('trade_result') or request.args.get('result')
    user = get_current_user()
    
    if not reference:
        flash('Payment submitted. Waiting for gateway confirmation.', 'info')
        return redirect(url_for('dashboard'))

    transaction = Transaction.query.filter_by(reference=reference, type='deposit', user_id=user.id).first()
    if not transaction:
        flash('Payment session not found. Please try again.', 'error')
        return redirect(url_for('deposit'))

    if trade_result not in (None, '', '1', 'SUCCESS', 'success'):
        flash('Payment was not successful. Please try again.', 'error')
        return redirect(url_for('deposit'))

    if transaction.status == 'completed':
        flash('Payment confirmed successfully.', 'success')
        return redirect(url_for('dashboard'))

    from datetime import datetime
    return render_template(
        'deposit/gtr_return.html',
        transaction=transaction,
        user=user,
        trade_no=trade_no,
        business_name='Max Wealth',
        current_time=datetime.now(),
    )

@app.route('/test/receipt')
@require_login
def test_receipt():
    """Create a test receipt for demonstration - REMOVE IN PRODUCTION"""
    user = get_current_user()
    
    # Create a sample transaction for testing
    from datetime import datetime
    import secrets
    
    timestamp = datetime.now().strftime("%m%d%H%M")
    reference = f"TEST{timestamp}{secrets.token_urlsafe(4)[:4].upper()}"
    
    test_transaction = Transaction(
        user_id=user.id,
        type='deposit',
        amount=5000.0,
        description='Test Deposit for Receipt Demo',
        status='completed',
        reference=reference,
        created_at=datetime.now()
    )
    
    db.session.add(test_transaction)
    db.session.commit()
    
    return redirect(url_for('deposit_receipt', reference=reference, trade_no=f'GTR{reference}'))

@app.route('/process_withdrawal', methods=['POST'])
@require_login
def process_withdrawal():
    """Handle withdrawal requests"""
    user = get_current_user()
    global SYSTEM_SETTINGS
    SYSTEM_SETTINGS = load_system_settings()
    active_pkg, has_bank_details = _withdrawal_access_state(user)
    access_block = _deny_withdrawal_access(active_pkg, has_bank_details, json_mode=request.is_json)
    if access_block:
        return access_block

    if not is_withdrawal_window_open(
        SYSTEM_SETTINGS['WITHDRAWAL_START_TIME'],
        SYSTEM_SETTINGS['WITHDRAWAL_END_TIME']
    ):
        lock_message = (
            f"Withdrawals are currently locked. The gateway is available only between "
            f"{format_withdrawal_window(SYSTEM_SETTINGS['WITHDRAWAL_START_TIME'], SYSTEM_SETTINGS['WITHDRAWAL_END_TIME'])}."
        )
        if request.is_json:
            return jsonify({'success': False, 'message': lock_message})
        flash(lock_message, 'error')
        return redirect(url_for('withdrawal'))
    
    try:
        # Handle JSON data
        if request.is_json:
            data = request.get_json()
            amount = data.get('amount')
            selected_bank_name = data.get('bank_name')
            other_bank = data.get('other_bank', '')
            account_number = data.get('account_number')
            account_name = data.get('account_name')
            purpose = data.get('purpose', '')
        else:
            # Handle form data (fallback)
            amount = request.form.get('amount')
            selected_bank_name = request.form.get('bank_name')
            other_bank = request.form.get('other_bank', '')
            account_number = request.form.get('account_number')
            account_name = request.form.get('account_name')
            purpose = request.form.get('purpose', '')

        if isinstance(selected_bank_name, str):
            selected_bank_name = selected_bank_name.strip()

        if selected_bank_name == 'Other':
            if not other_bank:
                if request.is_json:
                    return jsonify({'success': False, 'message': 'Please specify the bank name'})
                flash('Please specify the bank name', 'error')
                return redirect(url_for('request_withdrawal'))
            display_bank_name = other_bank.strip()
        else:
            display_bank_name = selected_bank_name

        if isinstance(other_bank, str):
            other_bank = other_bank.strip()
        
        if not all([amount, selected_bank_name, account_number, account_name]):
            if request.is_json:
                return jsonify({'success': False, 'message': 'All fields are required'})
            flash('All fields are required', 'error')
            return redirect(url_for('request_withdrawal'))
        
        try:
            amount = float(amount)
            
            # Check minimum withdrawal amount using system settings
            min_amount = SYSTEM_SETTINGS['MINIMUM_WITHDRAWAL']
            if amount < min_amount:
                if request.is_json:
                    return jsonify({'success': False, 'message': f'Minimum withdrawal amount is NGN{min_amount:,.0f}'})
                flash(f'Minimum withdrawal amount is NGN{min_amount:,.0f}', 'error')
                return redirect(url_for('request_withdrawal'))
            
            # Calculate withdrawal fee using system settings
            fee_percentage = SYSTEM_SETTINGS['WITHDRAWAL_FEE_PERCENTAGE']
            fee = amount * (fee_percentage / 100)
            net_amount = amount - fee  # What user actually receives
            total_deduction = amount  # Total amount deducted from balance
            
            # Check available balance
            if total_deduction > user.main_balance:
                if request.is_json:
                    return jsonify({'success': False, 'message': 'Insufficient balance'})
                flash('Insufficient balance', 'error')
                return redirect(url_for('request_withdrawal'))
            
            # Deduct the withdrawal amount from user's main balance
            user.main_balance -= total_deduction
            
            # Create withdrawal request
            withdrawal = Withdrawal(
                user_id=user.id,
                amount=amount,
                fee=fee,
                net_amount=net_amount,
                bank_name=selected_bank_name,
                other_bank=display_bank_name if selected_bank_name == 'Other' else None,
                account_number=account_number,
                account_name=account_name,
                status='pending'
            )
            
            db.session.add(withdrawal)
            db.session.flush()

            # Create transaction record for balance deduction
            transaction = Transaction(
                user_id=user.id,
                type='withdrawal',
                amount=-total_deduction,  # Negative because it's a deduction
                description=f'Withdrawal request: NGN{amount:,.0f} (Fee: NGN{fee:,.0f})',
                status='pending',
                reference=f'WD-{withdrawal.id:06d}'
            )
            
            db.session.add(transaction)
            db.session.commit()

            send_withdrawal_requested_email(
                user.name,
                user.email,
                amount,
                fee,
                fee_percentage,
                net_amount,
                display_bank_name,
                account_number,
                account_name,
                withdrawal.created_at.strftime('%Y-%m-%d %H:%M UTC') if withdrawal.created_at else utc_now().strftime('%Y-%m-%d %H:%M UTC'),
                build_public_url('dashboard'),
            )
            
            if request.is_json:
                return jsonify({
                    'success': True, 
                    'message': f'Withdrawal request submitted successfully: NGN{amount:,.0f} (Fee: NGN{fee:,.0f}, Net: NGN{net_amount:,.0f}). It will be reviewed by admin.'
                })
            
            flash(f'Withdrawal request submitted successfully: NGN{amount:,.0f} (Fee: NGN{fee:,.0f}, Net: NGN{net_amount:,.0f}). It will be reviewed by admin.', 'success')
            return redirect(url_for('withdrawal_receipt', withdrawal_id=withdrawal.id))
        
        except ValueError as e:
            print(f"ValueError in withdrawal processing: {str(e)}")
            if request.is_json:
                return jsonify({'success': False, 'message': 'Invalid amount format'})
            flash('Invalid amount format', 'error')
            return redirect(url_for('request_withdrawal'))
        except Exception as e:
            print(f"Exception in withdrawal processing: {str(e)}")
            db.session.rollback()
            if request.is_json:
                return jsonify({'success': False, 'message': f'An error occurred while processing your withdrawal: {str(e)}'})
            flash(f'An error occurred while processing your withdrawal: {str(e)}', 'error')
            return redirect(url_for('request_withdrawal'))
    
    except Exception as e:
        print(f"Outer exception in withdrawal processing: {str(e)}")
        db.session.rollback()
        if request.is_json:
            return jsonify({'success': False, 'message': f'An error occurred. Please try again: {str(e)}'})
        flash(f'An error occurred. Please try again: {str(e)}', 'error')
        return redirect(url_for('request_withdrawal'))

# Withdrawal receipt
@app.route('/withdrawal/receipt/<int:withdrawal_id>')
@require_login
def withdrawal_receipt(withdrawal_id):
    user = get_current_user()
    withdrawal = Withdrawal.query.filter_by(id=withdrawal_id, user_id=user.id).first()
    if not withdrawal:
        flash('Withdrawal not found.', 'error')
        return redirect(url_for('withdrawal'))

    # Map a pseudo-transaction object for template reuse patterns
    class Txn:
        pass
    txn = Txn()
    txn.reference = f"WD-{withdrawal.id:06d}"
    txn.created_at = withdrawal.created_at
    txn.status = 'completed' if withdrawal.status in ['completed', 'approved', 'processed'] else 'pending'
    txn.description = f"Withdrawal of NGN{withdrawal.amount:,.2f} (Fee NGN{withdrawal.fee:,.2f}, Net NGN{withdrawal.net_amount:,.2f})"
    txn.amount = -withdrawal.amount

    # Render a dedicated template for withdrawals (similar look to deposit receipt)
    return render_template(
        'withdrawal/receipt.html',
        user=user,
        withdrawal=withdrawal,
        transaction=txn,
        business_name='SONICVEST',
        current_time=datetime.now()
    )

# Blueprint-style route aliases for template compatibility
@app.route('/package.index')
def package_index():
    """Alias for packages route to match blueprint-style template references"""
    return packages()

@app.route('/package.buy')
def package_buy():
    """Alias for buy_package route to match blueprint-style template references"""
    package_id = request.args.get('id')
    if package_id:
        return buy_package(int(package_id))
    else:
        flash('Package not specified', 'error')
        return redirect(url_for('packages'))

@app.route('/dashboard.index')
def dashboard_index():
    """Alias for dashboard route to match blueprint-style template references"""
    return dashboard()

@app.route('/withdrawal.request_withdrawal', methods=['GET', 'POST'])
def withdrawal_request_withdrawal():
    """Alias for request_withdrawal route - redirect POST to process_withdrawal"""
    if request.method == 'POST':
        return process_withdrawal()
    return request_withdrawal()

@app.route('/referral.index')
def referral_index():
    """Alias for referral route to match blueprint-style template references"""
    return referral()

@app.route('/deposit.process')
def deposit_process():
    """Alias for process_deposit route to match blueprint-style template references"""
    return process_deposit()

# =============================================
# ADMIN PANEL ROUTES
# =============================================

# Admin User Model (simplified - using same User table with admin role)
def is_admin(user):
    """Check if user is admin"""
    return user and hasattr(user, 'is_admin') and user.is_admin

def require_admin(f):
    """Decorator to require admin access"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or not is_admin(user):
            flash('Access denied. Admin privileges required.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/admin')
@require_admin
def admin_dashboard():
    """Admin dashboard with overview stats"""
    try:
        # Get statistics
        total_users = User.query.count()
        active_packages = UserPackage.query.filter_by(is_active=True).count()
        total_deposits = db.session.query(db.func.sum(Transaction.amount)).filter(
            Transaction.type == 'deposit',
            Transaction.status == 'completed'
        ).scalar() or 0
        total_withdrawals = db.session.query(db.func.sum(Transaction.amount)).filter(
            Transaction.type == 'withdrawal',
            Transaction.status == 'completed'
        ).scalar() or 0
        pending_withdrawals = Transaction.query.filter(
            Transaction.type == 'withdrawal',
            Transaction.status == 'pending'
        ).count()
        
        # Recent transactions
        recent_transactions = Transaction.query.order_by(
            Transaction.created_at.desc()
        ).limit(10).all()
        
        # Recent users
        recent_users = User.query.order_by(User.created_at.desc()).limit(10).all()
        
        stats = {
            'total_users': total_users,
            'active_packages': active_packages,
            'total_deposits': total_deposits,
            'total_withdrawals': total_withdrawals,
            'pending_withdrawals': pending_withdrawals
        }
        
        return render_template('admin/dashboard.html', 
                             stats=stats,
                             recent_transactions=recent_transactions,
                             recent_users=recent_users)
    except Exception as e:
        flash(f'Error loading dashboard: {str(e)}', 'error')
        return redirect(url_for('login'))

@app.route('/admin/users')
@require_admin
def admin_users():
    """Admin users management"""
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    
    query = User.query
    if search:
        query = query.filter(
            db.or_(
                User.username.contains(search),
                User.email.contains(search),
                User.full_name.contains(search)
            )
        )
    
    users = query.order_by(User.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    
    return render_template('admin/users.html', users=users, search=search)

@app.route('/admin/transactions')
@require_admin
def admin_transactions():
    """Admin transactions management"""
    page = request.args.get('page', 1, type=int)
    transaction_type = request.args.get('type', '')
    status = request.args.get('status', '')
    
    query = Transaction.query
    if transaction_type:
        query = query.filter(Transaction.type == transaction_type)
    if status:
        query = query.filter(Transaction.status == status)
    
    transactions = query.order_by(Transaction.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    
    return render_template('admin/transactions.html', 
                         transactions=transactions,
                         transaction_type=transaction_type,
                         status=status)

@app.route('/admin/packages')
@require_admin
def admin_packages():
    """Admin packages management"""
    packages = Package.query.all()
    user_packages = UserPackage.query.filter_by(is_active=True).all()
    
    # Package statistics
    package_stats = {}
    for pkg in packages:
        active_count = UserPackage.query.filter_by(package_id=pkg.id, is_active=True).count()
        total_invested = db.session.query(db.func.sum(UserPackage.amount_invested)).filter(
            UserPackage.package_id == pkg.id
        ).scalar() or 0
        
        package_stats[pkg.id] = {
            'active_count': active_count,
            'total_invested': total_invested
        }
    
    return render_template('admin/packages.html', 
                         packages=packages,
                         user_packages=user_packages,
                         package_stats=package_stats)

@app.route('/admin/withdrawals')
@require_admin
def admin_withdrawals():
    """Admin withdrawals management"""
    status = request.args.get('status', 'pending')

    query = Withdrawal.query
    if status and status != 'all':
        if status == 'completed':
            query = query.filter(Withdrawal.status.in_(['approved', 'processed', 'completed']))
        else:
            query = query.filter(Withdrawal.status == status)

    withdrawals_raw = query.order_by(Withdrawal.created_at.desc()).all()

    withdrawals = []
    for withdrawal in withdrawals_raw:
        display_status = 'completed' if withdrawal.status in ['approved', 'processed'] else withdrawal.status
        withdrawals.append(type('WithdrawalView', (), {
            'id': withdrawal.id,
            'amount': withdrawal.amount,
            'net_amount': withdrawal.net_amount,
            'status': display_status,
            'created_at': withdrawal.created_at,
            'bank_name': withdrawal.bank_name,
            'other_bank': withdrawal.other_bank,
            'account_number': withdrawal.account_number,
            'account_name': withdrawal.account_name,
            'user': withdrawal.user,
        })())
    
    return render_template('admin/withdrawals.html', 
                         withdrawals=withdrawals,
                         status=status)

@app.route('/admin/approve_withdrawal/<int:transaction_id>', methods=['POST'])
@require_admin
def admin_approve_withdrawal(transaction_id):
    """Approve a withdrawal request"""
    try:
        transaction = Transaction.query.get_or_404(transaction_id)
        
        if transaction.type != 'withdrawal' or transaction.status != 'pending':
            flash('Invalid withdrawal transaction', 'error')
            return redirect(url_for('admin_withdrawals'))
        
        # Update transaction status
        transaction.status = 'completed'
        transaction.updated_at = utc_now()
        
        # Add admin note
        admin_user = get_current_user()
        transaction.admin_note = f"Approved by {admin_user.username} on {utc_now().strftime('%Y-%m-%d %H:%M:%S')}"

        if transaction.user:
            linked_withdrawal = Withdrawal.query.filter_by(
                user_id=transaction.user_id,
                status='pending'
            ).order_by(Withdrawal.created_at.desc()).first()

            if linked_withdrawal:
                linked_withdrawal.status = 'approved'
                linked_withdrawal.processed_at = utc_now()
                linked_withdrawal.processed_by = admin_user.id

                send_withdrawal_approved_email(
                    user_name=transaction.user.name,
                    user_email=transaction.user.email,
                    amount=linked_withdrawal.amount,
                    net_amount=linked_withdrawal.net_amount,
                    bank_name=linked_withdrawal.bank_name,
                    account_number=linked_withdrawal.account_number,
                    approval_date=utc_now().strftime('%Y-%m-%d %H:%M UTC'),
                    trace_id=f"WD-{transaction.id}",
                    dashboard_url=build_public_url('dashboard'),
                )
        
        db.session.commit()
        
        flash(f'Withdrawal of ₦{abs(transaction.amount):,.0f} approved successfully', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error approving withdrawal: {str(e)}', 'error')
    
    return redirect(url_for('admin_withdrawals'))

@app.route('/admin/reject_withdrawal/<int:transaction_id>', methods=['POST'])
@require_admin
def admin_reject_withdrawal(transaction_id):
    """Reject a withdrawal request"""
    try:
        transaction = Transaction.query.get_or_404(transaction_id)
        reason = request.form.get('reason', 'No reason provided')
        
        if transaction.type != 'withdrawal' or transaction.status != 'pending':
            flash('Invalid withdrawal transaction', 'error')
            return redirect(url_for('admin_withdrawals'))
        
        # Get user and refund the amount
        user = transaction.user
        user.withdrawal_balance += abs(transaction.amount)
        
        # Update transaction status
        transaction.status = 'rejected'
        transaction.updated_at = utc_now()
        
        # Add admin note
        admin_user = get_current_user()
        transaction.admin_note = f"Rejected by {admin_user.username}: {reason}"

        linked_withdrawal = Withdrawal.query.filter_by(
            user_id=transaction.user_id,
            status='pending'
        ).order_by(Withdrawal.created_at.desc()).first()

        if linked_withdrawal:
            linked_withdrawal.status = 'rejected'
            linked_withdrawal.reason = reason
            linked_withdrawal.processed_at = utc_now()
            linked_withdrawal.processed_by = admin_user.id

        if transaction.user:
            send_withdrawal_declined_email(
                user_name=transaction.user.name,
                user_email=transaction.user.email,
                amount=abs(transaction.amount),
                decline_date=utc_now().strftime('%Y-%m-%d %H:%M UTC'),
                decline_reason=reason,
                dashboard_url=build_public_url('dashboard'),
            )
        
        db.session.commit()
        
        flash(f'Withdrawal rejected and ₦{abs(transaction.amount):,.0f} refunded to user', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error rejecting withdrawal: {str(e)}', 'error')
    
    return redirect(url_for('admin_withdrawals'))

@app.route('/admin/api/stats')
@require_admin
def admin_api_stats():
    """API endpoint for admin dashboard stats"""
    try:
        stats = {
            'total_users': User.query.count(),
            'active_packages': UserPackage.query.filter_by(is_active=True).count(),
            'pending_withdrawals': Transaction.query.filter(
                Transaction.type == 'withdrawal',
                Transaction.status == 'pending'
            ).count(),
            'total_deposits': db.session.query(db.func.sum(Transaction.amount)).filter(
                Transaction.type == 'deposit',
                Transaction.status == 'completed'
            ).scalar() or 0,
            'total_withdrawals': db.session.query(db.func.sum(Transaction.amount)).filter(
                Transaction.type == 'withdrawal',
                Transaction.status == 'completed'
            ).scalar() or 0
        }
        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================
# API ENDPOINTS
# =============================================

@app.route('/api/get-referral-code', methods=['GET'])
def get_referral_code():
    """API endpoint to get current user's referral code"""
    try:
        if 'user_id' in session:
            user = User.query.get(session['user_id'])
            if user:
                return jsonify({
                    'success': True,
                    'referral_code': user.referral_code,
                    'username': user.username
                })
        
        # Return default/guest referral code if not logged in
        return jsonify({
            'success': False,
            'referral_code': 'GUEST001',
            'message': 'Not logged in'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'referral_code': 'ERROR001',
            'error': str(e)
        }), 500

# =============================================
# END ADMIN PANEL ROUTES
# =============================================

@app.route('/process_incomes')
@require_login
def process_incomes():
    """Process income payouts for all active packages (for testing)"""
    if not get_current_user().is_admin:
        flash('Access denied', 'error')
        return redirect(url_for('dashboard'))
    
    try:
        processed = 0
        current_time = utc_now()
        
        # Get all active user packages
        active_packages = UserPackage.query.filter(
            UserPackage.is_active == True,
            UserPackage.end_date > current_time
        ).all()
        
        for user_package in active_packages:
            # Check if enough time has passed since last payout
            hours_since_last_payout = SYSTEM_SETTINGS['INCOME_DROP_HOURS']
            
            if user_package.last_payout is None:
                # First payout - check if package started more than specified hours ago
                time_since_start = current_time - user_package.start_date
                if time_since_start.total_seconds() >= hours_since_last_payout * 3600:
                    should_payout = True
                else:
                    should_payout = False
            else:
                # Check time since last payout
                time_since_last = current_time - user_package.last_payout
                should_payout = time_since_last.total_seconds() >= hours_since_last_payout * 3600
            
            if should_payout:
                # Process the payout
                user = User.query.get(user_package.user_id)
                payout_amount = user_package.daily_return
                
                # Add to user's main balance
                user.main_balance += payout_amount
                user.total_earned += payout_amount
                
                # Update package's total earned
                user_package.total_earned = (user_package.total_earned or 0) + payout_amount
                
                # Update last payout time
                user_package.last_payout = current_time
                
                # Create transaction record
                transaction = Transaction(
                    user_id=user.id,
                    type='package_income',
                    amount=payout_amount,
                    description=f'Income from {user_package.package.name}',
                    status='completed'
                )
                
                db.session.add(transaction)
                processed += 1
        
        db.session.commit()
        flash(f'✅ Processed {processed} income payouts successfully!', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'❌ Error processing incomes: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/cron/process-income-payouts', methods=['GET', 'POST'])
def cron_process_income_payouts():
    """
    Cron endpoint to process income payouts for all active packages
    This endpoint should be called by an external cron service
    
    For security, it checks for a CRON_SECRET token in the request headers or query params
    Set CRON_SECRET in your environment variables
    
    Setup instructions:
    1. Set CRON_SECRET in your hosting environment variables
    2. Add a cron job to call: https://yourdomain.com/cron/process-income-payouts?token=YOUR_CRON_SECRET
    3. Recommended frequency: Every 30 minutes to 1 hour
    
    Examples:
    - Vercel Cron: Use vercel.json with cron config
    - Heroku Scheduler: Add job `curl https://yourdomain.com/cron/process-income-payouts?token=$CRON_SECRET`
    - Render Cron Jobs: Use Render dashboard to add cron job
    - EasyCron / Cron-Job.org: Add URL with token parameter
    """
    # Check for authentication token
    cron_secret = os.environ.get('CRON_SECRET')
    
    # Get token from header or query param
    provided_token = request.headers.get('X-Cron-Token') or request.args.get('token')
    
    # If CRON_SECRET is not set, allow access (for development/testing)
    # In production, ALWAYS set CRON_SECRET for security
    if cron_secret and provided_token != cron_secret:
        return jsonify({
            'success': False,
            'error': 'Unauthorized - Invalid or missing token',
            'message': 'Set CRON_SECRET in environment variables and provide it as X-Cron-Token header or ?token= query param'
        }), 401
    
    try:
        processed = 0
        errors = []
        current_time = utc_now()
        
        # Get all active user packages
        active_packages = UserPackage.query.filter(
            UserPackage.is_active == True,
            UserPackage.end_date > current_time
        ).all()
        
        for user_package in active_packages:
            try:
                # Check if enough time has passed since last payout
                hours_since_last_payout = SYSTEM_SETTINGS['INCOME_DROP_HOURS']
                
                if user_package.last_payout is None:
                    # First payout - check if package started more than specified hours ago
                    time_since_start = current_time - user_package.start_date
                    if time_since_start.total_seconds() >= hours_since_last_payout * 3600:
                        should_payout = True
                    else:
                        should_payout = False
                else:
                    # Check time since last payout
                    time_since_last = current_time - user_package.last_payout
                    should_payout = time_since_last.total_seconds() >= hours_since_last_payout * 3600
                
                if should_payout:
                    # Process the payout
                    user = User.query.get(user_package.user_id)
                    if not user:
                        errors.append(f"User not found for package {user_package.id}")
                        continue
                    
                    payout_amount = user_package.daily_return
                    
                    # Add to user's main balance
                    user.main_balance += payout_amount
                    user.total_earned += payout_amount
                    
                    # Update package's total earned
                    user_package.total_earned = (user_package.total_earned or 0) + payout_amount
                    
                    # Update last payout time
                    user_package.last_payout = current_time
                    
                    # Create transaction record
                    transaction = Transaction(
                        user_id=user.id,
                        type='package_income',
                        amount=payout_amount,
                        description=f'Daily income from {user_package.package.name}',
                        status='completed'
                    )
                    
                    db.session.add(transaction)
                    processed += 1
                    
            except Exception as package_error:
                errors.append(f"Error processing package {user_package.id}: {str(package_error)}")
                continue
        
        db.session.commit()
        
        response_data = {
            'success': True,
            'processed': processed,
            'timestamp': current_time.isoformat(),
            'total_active_packages': len(active_packages)
        }
        
        if errors:
            response_data['errors'] = errors
        
        print(f"✅ Cron job processed {processed} income payouts at {current_time}")
        
        return jsonify(response_data), 200
        
    except Exception as e:
        db.session.rollback()
        error_message = str(e)
        print(f"❌ Cron job error: {error_message}")
        
        return jsonify({
            'success': False,
            'error': error_message,
            'timestamp': utc_now().isoformat()
        }), 500

@app.route('/test_loaders')
@require_login
def test_loaders():
    """Test page for loader functionality"""
    return render_template('test_loaders.html', user=get_current_user())

@app.route('/api/dashboard-updates')
@require_login
def dashboard_updates():
    """API endpoint to get real-time dashboard updates"""
    user = get_current_user()
    
    # Get recent transactions (income drops)
    recent_transactions = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.created_at.desc()).limit(5).all()
    
    # Get active packages
    active_packages = UserPackage.query.filter(
        UserPackage.user_id == user.id,
        UserPackage.is_active == True,
        UserPackage.end_date > utc_now()
    ).all()
    
    # Format transactions for JSON
    transactions_data = []
    for transaction in recent_transactions:
        transactions_data.append({
            'id': transaction.id,
            'type': transaction.type,
            'amount': float(transaction.amount),
            'description': transaction.description,
            'status': transaction.status,
            'created_at': transaction.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'formatted_amount': f"₦{transaction.amount:,.0f}"
        })
    
    # Format active packages for JSON
    packages_data = []
    for user_package in active_packages:
        package = user_package.package
        time_remaining = user_package.end_date - utc_now()
        days_remaining = max(0, time_remaining.days)
        
        # Calculate next payout time
        next_payout = None
        if user_package.last_payout:
            next_payout_time = user_package.last_payout + timedelta(hours=SYSTEM_SETTINGS['INCOME_DROP_HOURS'])
            if next_payout_time > utc_now():
                time_until_next = next_payout_time - utc_now()
                minutes_remaining = int(time_until_next.total_seconds() / 60)
                next_payout = f"{minutes_remaining} minutes"
            else:
                next_payout = "Due now"
        else:
            # First payout
            first_payout_time = user_package.start_date + timedelta(hours=SYSTEM_SETTINGS['INCOME_DROP_HOURS'])
            if first_payout_time > utc_now():
                time_until_first = first_payout_time - utc_now()
                minutes_remaining = int(time_until_first.total_seconds() / 60)
                next_payout = f"{minutes_remaining} minutes"
            else:
                next_payout = "Due now"
        
        packages_data.append({
            'id': user_package.id,
            'name': package.name,
            'daily_return': float(user_package.daily_return),
            'formatted_daily_return': f"₦{user_package.daily_return:,.0f}",
            'days_remaining': days_remaining,
            'next_payout': next_payout,
            'last_payout': user_package.last_payout.strftime('%Y-%m-%d %H:%M:%S') if user_package.last_payout else None
        })
    
    return jsonify({
        'success': True,
        'user': {
            'main_balance': float(user.main_balance),
            'recharge_balance': float(user.recharge_balance),
            'total_earned': float(user.total_earned),
            'formatted_main_balance': f"₦{user.main_balance:,.0f}",
            'formatted_recharge_balance': f"₦{user.recharge_balance:,.0f}",
            'formatted_total_earned': f"₦{user.total_earned:,.0f}"
        },
        'transactions': transactions_data,
        'active_packages': packages_data,
        'timestamp': utc_now().strftime('%Y-%m-%d %H:%M:%S')
    })

@app.route('/health')
def health_check():
    """Health check endpoint for monitoring"""
    try:
        # Check database connection
        from sqlalchemy import text
        from datetime import timezone
        db.session.execute(text('SELECT 1'))
        db_status = 'healthy'
    except Exception as e:
        db_status = f'unhealthy: {str(e)}'
    
    # Check active packages count
    try:
        active_count = UserPackage.query.filter(
            UserPackage.is_active == True,
            UserPackage.end_date > datetime.now(timezone.utc)
        ).count()
    except:
        active_count = 'unknown'
    
    return jsonify({
        'status': 'ok' if db_status == 'healthy' else 'degraded',
        'database': db_status,
        'active_packages': active_count,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/worker-status')
def worker_status():
    """Check worker status - useful for debugging"""
    try:
        from datetime import timezone
        from sqlalchemy import text
        
        # Get recent income transactions (last hour)
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        recent_income = Transaction.query.filter(
            Transaction.type == 'package_income',
            Transaction.created_at >= one_hour_ago
        ).count()
        
        # Get packages due for payout
        current_time = datetime.now(timezone.utc)
        packages_due = 0
        active_packages = UserPackage.query.filter(
            UserPackage.is_active == True,
            UserPackage.end_date > current_time
        ).all()
        
        for pkg in active_packages:
            hours_since_payout = SYSTEM_SETTINGS['INCOME_DROP_HOURS']
            
            # Convert naive datetimes to UTC aware if needed
            start_date = pkg.start_date.replace(tzinfo=timezone.utc) if pkg.start_date.tzinfo is None else pkg.start_date
            last_payout = pkg.last_payout.replace(tzinfo=timezone.utc) if pkg.last_payout and pkg.last_payout.tzinfo is None else pkg.last_payout
            
            if last_payout is None:
                time_since_start = current_time - start_date
                if time_since_start.total_seconds() >= hours_since_payout * 3600:
                    packages_due += 1
            else:
                time_since_last = current_time - last_payout
                if time_since_last.total_seconds() >= hours_since_payout * 3600:
                    packages_due += 1
        
        return jsonify({
            'status': 'ok',
            'recent_payouts_1h': recent_income,
            'packages_due': packages_due,
            'total_active_packages': len(active_packages),
            'worker_expected': 'running separately',
            'check_interval': '30 seconds',
            'payout_interval': f"{SYSTEM_SETTINGS['INCOME_DROP_HOURS']} hours",
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

def background_income_processor():
    """
    Background task to automatically process income payouts
    
    ⚠️ WARNING: This only works in local development when running `python app.py`
    In production (Gunicorn, Vercel, Heroku, etc.), this thread will NOT start
    because production servers don't execute the if __name__ == '__main__' block.
    
    For production, use the /cron/process-income-payouts endpoint with a cron job instead.
    See CRON_SETUP_GUIDE.md for instructions.
    """
    print("⚠️  Background income processor starting (LOCAL DEVELOPMENT ONLY)")
    print("⚠️  For production, set up a cron job - see CRON_SETUP_GUIDE.md")
    
    while True:
        try:
            with app.app_context():
                current_time = utc_now()
                processed = 0
                
                # Get all active user packages
                active_packages = UserPackage.query.filter(
                    UserPackage.is_active == True,
                    UserPackage.end_date > current_time
                ).all()
                
                for user_package in active_packages:
                    # Check if enough time has passed since last payout
                    hours_since_last_payout = SYSTEM_SETTINGS['INCOME_DROP_HOURS']
                    
                    if user_package.last_payout is None:
                        # First payout - check if package started more than specified hours ago
                        time_since_start = current_time - user_package.start_date
                        if time_since_start.total_seconds() >= hours_since_last_payout * 3600:
                            should_payout = True
                        else:
                            should_payout = False
                    else:
                        # Check time since last payout
                        time_since_last = current_time - user_package.last_payout
                        should_payout = time_since_last.total_seconds() >= hours_since_last_payout * 3600
                    
                    if should_payout:
                        # Process the payout
                        user = User.query.get(user_package.user_id)
                        payout_amount = user_package.daily_return
                        
                        # Add to user's main balance
                        user.main_balance += payout_amount
                        user.total_earned += payout_amount
                        
                        # Update package's total earned
                        user_package.total_earned = (user_package.total_earned or 0) + payout_amount
                        
                        # Update last payout time
                        user_package.last_payout = current_time
                        
                        # Create transaction record
                        transaction = Transaction(
                            user_id=user.id,
                            type='package_income',
                            amount=payout_amount,
                            description=f'Income from {user_package.package.name}',
                            status='completed'
                        )
                        
                        db.session.add(transaction)
                        processed += 1
                
                if processed > 0:
                    db.session.commit()
                    print(f"✅ Background processor: {processed} income payouts processed at {current_time}")
                
        except Exception as e:
            print(f"❌ Background income processor error: {e}")
            if 'db' in locals():
                db.session.rollback()
        
        # Sleep for 30 seconds before next check
        time.sleep(30)

if __name__ == '__main__':
    print("🗄️  Initializing database...")
    try:
        with app.app_context():
            # Try to create all tables
            db.create_all()
            ensure_system_settings_schema()
            ensure_withdrawal_schema()
            
            # Initialize admin database
            from admin_routes import init_admin_db
            init_admin_db()
            print("✅ Admin database initialized")
            
            # Load system settings from DB
            SYSTEM_SETTINGS = load_system_settings()
            print("✅ System settings loaded from DB")
            
            print("✅ Database tables created successfully")
        
        print("\n" + "="*70)
        print("⚠️  IMPORTANT: Background income processor only works in LOCAL development")
        print("⚠️  Production servers (Gunicorn, Vercel, etc.) won't run this thread!")
        print("="*70)
        print("\n📋 For PRODUCTION income payouts, you MUST set up a cron job:")
        print("   1. Set CRON_SECRET environment variable on your hosting platform")
        print("   2. Add a cron job to call: /cron/process-income-payouts?token=YOUR_SECRET")
        print("   3. Recommended frequency: Every 30-60 minutes")
        print("\n📖 See CRON_SETUP_GUIDE.md for detailed instructions\n")
        print("="*70 + "\n")
        
        # Start background income processor (LOCAL DEVELOPMENT ONLY)
        income_thread = threading.Thread(target=background_income_processor, daemon=True)
        income_thread.start()
        print("🚀 Background income processor started (LOCAL DEVELOPMENT ONLY)")
        
        # Force use port 5002 to avoid conflicts
        port = 5002
        debug = os.environ.get('FLASK_ENV', 'development') == 'development'
        host = '0.0.0.0'  # Listen on all interfaces for hosting platforms
        
        print(f"🚀 Starting Flask application on http://0.0.0.0:{port}")
        print(f"🚀 Admin panel at http://0.0.0.0:{port}/admin/login")
        app.run(debug=debug, host=host, port=port)
        
    except Exception as e:
        print(f"❌ Startup error: {e}")
        print("💡 Check if another process is using port 5000")
