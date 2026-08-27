from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from flask_mail import Message
import threading
import os
from gtr_services_clean import GTRPayService
from admin_db import (
    execute_query, execute_scalar, execute_one, execute_all, 
    execute_insert, execute_update, dict_from_row, set_db
)

# Create admin blueprint
admin_bp = Blueprint('admin', __name__, 
                    url_prefix='/admin',
                    template_folder='templates/admin',
                    static_folder='static')

# Database will be set from app.py
db = None
gateway_service = GTRPayService()


def _send_admin_mail_async(subject, recipient, html_template, text_template, context, success_log, error_log):
    """Send transactional email from admin routes without blocking API responses."""
    app_obj = current_app._get_current_object()

    def _send():
        try:
            if not app_obj.config.get('MAIL_USERNAME') or not app_obj.config.get('MAIL_PASSWORD'):
                print(f"⚠️  {success_log} skipped: MAIL_USERNAME or MAIL_PASSWORD is missing.")
                return

            mail_ext = app_obj.extensions.get('mail')
            if not mail_ext:
                print(f"⚠️  {success_log} skipped: Flask-Mail extension is not initialized.")
                return

            with app_obj.app_context():
                html_body = render_template(html_template, **context)
                text_body = render_template(text_template, **context)
                msg = Message(
                    subject=subject,
                    recipients=[recipient],
                    sender=app_obj.config.get('MAIL_DEFAULT_SENDER'),
                    body=text_body,
                    html=html_body,
                )
                mail_ext.send(msg)
                print(f"✅ {success_log} sent to {recipient}")
        except Exception as exc:
            print(f"⚠️  Could not send {error_log} to {recipient}: {exc}")

    threading.Thread(target=_send, daemon=True).start()

def init_admin_routes(database):
    """Initialize admin routes with database connection"""
    global db
    db = database
    set_db(database)

def init_admin_db():
    """Initialize admin database tables with SQLAlchemy"""
    global db
    if db is None:
        try:
            from app import db as app_db
            db = app_db
            set_db(db)
        except ImportError:
            return
    
    try:
        with db.app.app_context():
            # Create all tables
            db.create_all()
            
            # Create admin_users table for PostgreSQL/SQLite
            create_admin_table_sql = '''
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
            '''
            
            try:
                execute_update(create_admin_table_sql)
            except:
                # If table creation fails, it likely already exists
                pass
            
            # Check if default admin exists
            try:
                check_admin_sql = "SELECT COUNT(*) FROM admin_users"
                count = execute_scalar(check_admin_sql)
                if not count or count == 0:
                    admin_password = generate_password_hash('admin123')
                    insert_admin_sql = '''
                        INSERT INTO admin_users (username, email, password_hash, role)
                        VALUES (:username, :email, :password, :role)
                    '''
                    params = {
                        'username': 'admin',
                        'email': 'admin@fintech.com',
                        'password': admin_password,
                        'role': 'super_admin'
                    }
                    from admin_db import execute_insert
                    execute_insert(insert_admin_sql, params)
                    print("✅ Default admin user created: admin / admin123")
            except Exception as e:
                print(f"⚠️  Admin user check: {e}")
    except Exception as e:
        print(f"⚠️  Admin DB initialization: {e}")

def require_admin_login(f):
    """Decorator to require admin login"""
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            flash('Please log in to access the admin panel.', 'warning')
            return redirect(url_for('admin.login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

@admin_bp.route('/settings', methods=['GET', 'POST'])
@require_admin_login
def settings():
    """Admin settings page"""
    # Convert dict to object-like for template
    class DictObj:
        def __init__(self, d):
            if not d:
                d = {}
            self.__dict__.update(d)
    
    # Get bank details
    bank_sql = 'SELECT * FROM bank_details LIMIT 1'
    bank_details_dict = execute_one(bank_sql)
    
    if not bank_details_dict:
        # Create default bank details
        insert_sql = '''
            INSERT INTO bank_details (bank_name, account_number, account_name, is_active, created_at)
            VALUES (:bank_name, :account_number, :account_name, TRUE, CURRENT_TIMESTAMP)
        '''
        execute_insert(insert_sql, {
            'bank_name': '',
            'account_number': '',
            'account_name': ''
        })
        bank_details_dict = execute_one(bank_sql)
    
    # Get system settings
    settings_sql = 'SELECT * FROM system_settings LIMIT 1'
    system_settings_dict = execute_one(settings_sql)
    
    if not system_settings_dict:
        # Create default system settings
        insert_sql = '''
            INSERT INTO system_settings (
                welcome_bonus, minimum_deposit, minimum_withdrawal,
                daily_checkin_bonus, withdrawal_fee_percentage, income_drop_hours,
                withdrawal_start_time, withdrawal_end_time,
                referral_level1, referral_level2, referral_level3,
                is_active, created_at
            ) VALUES (
                :welcome_bonus, :minimum_deposit, :minimum_withdrawal,
                :daily_checkin_bonus, :withdrawal_fee_percentage, :income_drop_hours,
                :withdrawal_start_time, :withdrawal_end_time,
                :referral_level1, :referral_level2, :referral_level3,
                1, CURRENT_TIMESTAMP
            )
        '''
        execute_insert(insert_sql, {
            'welcome_bonus': 100.0,
            'minimum_deposit': 3000.0,
            'minimum_withdrawal': 2200.0,
            'daily_checkin_bonus': 100.0,
            'withdrawal_fee_percentage': 11.0,
            'income_drop_hours': 24.0,
            'withdrawal_start_time': 9,
            'withdrawal_end_time': 17,
            'referral_level1': 0.15,
            'referral_level2': 0.03,
            'referral_level3': 0.01,
            'deposit_gateway_mode': 'automatic'
        })
        system_settings_dict = execute_one(settings_sql)
    
    bank_details = DictObj(bank_details_dict)
    system_settings = DictObj(system_settings_dict)
    
    if request.method == 'POST':
        save_failed = False

        # Handle bank details
        if 'save_bank' in request.form:
            bank_name = request.form.get('bank_name', '').strip()
            account_number = request.form.get('account_number', '').strip()
            account_name = request.form.get('account_name', '').strip()

            if bank_details_dict and bank_details_dict.get('id'):
                update_sql = '''
                    UPDATE bank_details
                    SET bank_name = :bank_name,
                        account_number = :account_number,
                        account_name = :account_name
                    WHERE id = :id
                '''
                saved = execute_update(update_sql, {
                    'id': bank_details_dict['id'],
                    'bank_name': bank_name,
                    'account_number': account_number,
                    'account_name': account_name
                })
            else:
                insert_sql = '''
                    INSERT INTO bank_details (bank_name, account_number, account_name, is_active, created_at)
                    VALUES (:bank_name, :account_number, :account_name, TRUE, CURRENT_TIMESTAMP)
                '''
                saved = execute_insert(insert_sql, {
                    'bank_name': bank_name,
                    'account_number': account_number,
                    'account_name': account_name
                })

            if saved:
                flash('Bank details saved successfully!', 'success')
                bank_details_dict = execute_one(bank_sql)
                bank_details = DictObj(bank_details_dict)
            else:
                save_failed = True
                flash('Failed to save bank details.', 'error')
        
        # Handle system settings
        if 'save_system' in request.form:
            deposit_gateway_mode = request.form.get('deposit_gateway_mode', 'automatic').strip().lower()
            if deposit_gateway_mode not in ('manual', 'automatic'):
                deposit_gateway_mode = 'automatic'

            system_values = {
                'welcome_bonus': float(request.form.get('welcome_bonus', 100.0)),
                'minimum_deposit': float(request.form.get('minimum_deposit', 3000.0)),
                'minimum_withdrawal': float(request.form.get('minimum_withdrawal', 2200.0)),
                'daily_checkin_bonus': float(request.form.get('daily_checkin_bonus', 20.0)),
                'withdrawal_fee_percentage': float(request.form.get('withdrawal_fee_percentage', 11.0)),
                'income_drop_hours': float(request.form.get('income_drop_hours', 24.0)),
                'withdrawal_start_time': int(request.form.get('withdrawal_start_time', 9)),
                'withdrawal_end_time': int(request.form.get('withdrawal_end_time', 17)),
                'referral_level1': float(request.form.get('referral_level1', 15)) / 100.0,
                'referral_level2': float(request.form.get('referral_level2', 3)) / 100.0,
                'referral_level3': float(request.form.get('referral_level3', 1)) / 100.0,
                'deposit_gateway_mode': deposit_gateway_mode
            }

            if system_settings_dict and system_settings_dict.get('id'):
                update_sql = '''
                    UPDATE system_settings
                    SET welcome_bonus = :welcome_bonus,
                        minimum_deposit = :minimum_deposit,
                        minimum_withdrawal = :minimum_withdrawal,
                        daily_checkin_bonus = :daily_checkin_bonus,
                        withdrawal_fee_percentage = :withdrawal_fee_percentage,
                        income_drop_hours = :income_drop_hours,
                        withdrawal_start_time = :withdrawal_start_time,
                        withdrawal_end_time = :withdrawal_end_time,
                        referral_level1 = :referral_level1,
                        referral_level2 = :referral_level2,
                        referral_level3 = :referral_level3,
                        deposit_gateway_mode = :deposit_gateway_mode
                    WHERE id = :id
                '''
                saved = execute_update(update_sql, {'id': system_settings_dict['id'], **system_values})
            else:
                insert_sql = '''
                    INSERT INTO system_settings (
                        welcome_bonus, minimum_deposit, minimum_withdrawal,
                        daily_checkin_bonus, withdrawal_fee_percentage, income_drop_hours,
                        withdrawal_start_time, withdrawal_end_time,
                        referral_level1, referral_level2, referral_level3,
                        deposit_gateway_mode,
                        is_active, created_at
                    ) VALUES (
                        :welcome_bonus, :minimum_deposit, :minimum_withdrawal,
                        :daily_checkin_bonus, :withdrawal_fee_percentage, :income_drop_hours,
                        :withdrawal_start_time, :withdrawal_end_time,
                        :referral_level1, :referral_level2, :referral_level3,
                        :deposit_gateway_mode,
                        TRUE, CURRENT_TIMESTAMP
                    )
                '''
                saved = execute_insert(insert_sql, system_values)

            if saved:
                flash('System settings saved successfully!', 'success')
                system_settings_dict = execute_one(settings_sql)
                system_settings = DictObj(system_settings_dict)

                # Refresh the in-process cache used by the main app.
                try:
                    import app as app_module
                    app_module.SYSTEM_SETTINGS = app_module.load_system_settings()
                except Exception as refresh_error:
                    print(f"⚠️  Could not refresh app SYSTEM_SETTINGS: {refresh_error}")
            else:
                save_failed = True
                flash('Failed to save system settings.', 'error')
        
        return redirect(url_for('admin.settings'))
    
    return render_template('admin/settings.html', 
                          admin=get_current_admin(),
                          bank_details=bank_details,
                          system_settings=system_settings)

class DatabaseAdapter:
    """Adapter to provide a connection-like interface using db.session"""
    def __init__(self, database=None):
        self.db = database
        self.lastrowid = None
        if self.db is None:
            # Try to get db from app
            try:
                from app import db as app_db
                self.db = app_db
            except ImportError:
                pass
    
    def _convert_query_and_params(self, query, params=None):
        """Convert SQLite-style SQL to PostgreSQL-style"""
        if params is None:
            return query, {}
        
        # Skip BEGIN/COMMIT as they're handled by transaction management
        if query.strip().upper() in ('BEGIN', 'COMMIT', 'ROLLBACK'):
            return query, {}
        
        # Convert SQLite string functions first
        converted_query = query.replace("datetime('now')", "CURRENT_TIMESTAMP")
        converted_query = converted_query.replace("DATE('now')", "CURRENT_DATE")
        converted_query = converted_query.replace("date('now')", "CURRENT_DATE")
        
        # Quote all table names that might be reserved words - be very aggressive
        import re
        
        # Handle: FROM user, LEFT JOIN user, JOIN user, etc.
        # Only quote if not already quoted via negative lookahead/lookbehind
        converted_query = re.sub(r'(?<!")(\buser\b)(?!")', r'"\1"', converted_query, flags=re.IGNORECASE)
        
        # Handle "transaction" table (reserved word)
        # Only quote if not already quoted
        converted_query = re.sub(r'(?<!")(\btransaction\b)(?!")', r'"\1"', converted_query, flags=re.IGNORECASE)
        
        # Convert LIKE to ILIKE for PostgreSQL (case-insensitive)
        converted_query = converted_query.replace('LIKE ?', 'ILIKE :param').replace('LIKE', 'ILIKE')
        
        # Convert WHERE is_active = 1 to WHERE is_active = true
        converted_query = converted_query.replace('is_active = 1', 'is_active = true')
        converted_query = converted_query.replace('is_active = 0', 'is_active = false')
        
        # Also convert in VALUES clauses for INSERT statements
        converted_query = converted_query.replace(', 1, CURRENT', ', true, CURRENT')
        converted_query = converted_query.replace(', 1)', ', true)')
        
        # Add RETURNING id for INSERT statements (PostgreSQL)
        if 'INSERT' in converted_query.upper() and 'RETURNING' not in converted_query.upper():
            converted_query = converted_query.rstrip(';') + ' RETURNING id;'
        
        # Convert ? placeholders to :param1, :param2, etc.
        if isinstance(params, (list, tuple)):
            converted_params = {}
            parts = converted_query.split('?')
            new_query = ""
            for i in range(len(params)):
                new_query += parts[i] + f':p{i}'
            new_query += parts[-1]
            
            for i, param in enumerate(params):
                converted_params[f'p{i}'] = param
            return new_query, converted_params
        
        return converted_query, params
    
    def execute(self, query, params=None):
        from sqlalchemy import text
        try:
            if self.db is None:
                print("❌ Database not initialized")
                return AdapterCursor(None)
            
            # Skip transaction control statements
            if query.strip().upper() in ('BEGIN', 'COMMIT', 'ROLLBACK'):
                return AdapterCursor(None)
            
            # Convert query and parameters
            converted_query, converted_params = self._convert_query_and_params(query, params)
            
            try:
                if converted_params:
                    result = self.db.session.execute(text(converted_query), converted_params)
                else:
                    result = self.db.session.execute(text(converted_query))
                return AdapterCursor(result)
            except Exception as e:
                # Check if transaction is aborted
                if 'InFailedSqlTransaction' in str(type(e).__name__) or 'aborted' in str(e).lower():
                    print(f"⚠️  Transaction aborted, rolling back...")
                    try:
                        self.db.session.rollback()
                    except:
                        pass
                    # Return empty cursor
                    return AdapterCursor(None)
                else:
                    raise
        except Exception as e:
            print(f"❌ Database error: {e}")
            return AdapterCursor(None)
    
    def commit(self):
        try:
            if self.db:
                self.db.session.commit()
        except Exception as e:
            print(f"❌ Commit error: {e}")
            if self.db:
                self.db.session.rollback()
    
    def rollback(self):
        """Rollback current transaction"""
        try:
            if self.db:
                self.db.session.rollback()
        except Exception as e:
            print(f"❌ Rollback error: {e}")
    
    def cursor(self):
        """Return self to maintain compatibility with cursor-based code"""
        return self
    
    def close(self):
        pass

class AdapterCursor:
    """Cursor-like object that wraps SQLAlchemy results"""
    def __init__(self, result):
        self.result = result
        self._rows = None
        
    def fetchone(self):
        if self.result is None:
            return None
        try:
            row = self.result.fetchone()
            if row:
                return AdapterRow(row._mapping if hasattr(row, '_mapping') else dict(row))
            return None
        except:
            return None
    
    def fetchall(self):
        if self.result is None:
            return []
        try:
            rows = self.result.fetchall()
            return [AdapterRow(row._mapping if hasattr(row, '_mapping') else dict(row)) for row in rows]
        except:
            return []

class AdapterRow:
    """Row object that supports both dict-like and attribute access"""
    def __init__(self, data):
        self._data = data if isinstance(data, dict) else dict(data)
    
    def __getitem__(self, key):
        return self._data.get(key)
    
    def __getattr__(self, key):
        if key.startswith('_'):
            return super().__getattribute__(key)
        return self._data.get(key)
    
    def get(self, key, default=None):
        return self._data.get(key, default)
    
    def keys(self):
        return self._data.keys()

def get_db_connection():
    """Get a database connection adapter"""
    global db
    if db is None:
        # Try to import from app
        try:
            from app import db as app_db
            db = app_db
        except ImportError:
            pass
    return DatabaseAdapter(db)

def get_current_admin():
    """Get current admin user"""
    if 'admin_id' in session:
        admin_sql = 'SELECT * FROM admin_users WHERE id = :id AND is_active = true'
        admin = execute_one(admin_sql, {'id': session['admin_id']})
        return admin
    return None

# --- Admin Impersonation Helpers ---
def is_impersonating():
    return session.get('impersonating') is True and 'user_id' in session and 'admin_id' in session

def start_impersonation(target_user):
    """Begin impersonation of a user"""
    admin = get_current_admin()
    session['impersonating'] = True
    session['impersonated_by_admin_id'] = admin.get('id') if admin else None
    session['impersonated_admin_username'] = admin.get('username') if admin else None
    
    # Handle both dict and object-like targets
    user_name = None
    if isinstance(target_user, dict):
        user_name = target_user.get('name') or target_user.get('username')
    else:
        user_name = getattr(target_user, 'name', None) or getattr(target_user, 'username', None)
    
    session['impersonated_user_name'] = user_name or f"User #{target_user.get('id') or target_user.id}"
    session['user_id'] = target_user.get('id') if isinstance(target_user, dict) else target_user.id

def stop_impersonation_session():
    """Clear impersonation flags and leave admin session intact."""
    session.pop('impersonating', None)
    session.pop('impersonated_by_admin_id', None)
    session.pop('impersonated_admin_username', None)
    session.pop('impersonated_user_name', None)
    # Remove user session to avoid staying as that user
    session.pop('user_id', None)

# Admin Routes
@admin_bp.route('/')
@admin_bp.route('/dashboard')
@require_admin_login
def dashboard():
    """Admin dashboard"""
    stats = {}
    
    # User statistics
    users_count = execute_scalar('SELECT COUNT(*) FROM "user"')
    stats['total_users'] = users_count or 0
    
    # Deposits statistics
    deposits_total = execute_scalar('SELECT COALESCE(SUM(amount), 0) FROM "transaction" WHERE type = \'deposit\'')
    stats['total_deposits'] = deposits_total or 0
    
    # Withdrawals statistics
    withdrawals_total = execute_scalar('SELECT COALESCE(SUM(amount), 0) FROM withdrawal WHERE status = \'completed\'')
    stats['total_withdrawals'] = withdrawals_total or 0
    
    # Pending withdrawals count
    pending_count = execute_scalar('SELECT COUNT(*) FROM withdrawal WHERE status = \'pending\'')
    stats['pending_withdrawals'] = pending_count or 0
    
    # Recent users
    recent_users_sql = '''
        SELECT id, name as username, email, created_at as registration_date, is_active
        FROM "user" 
        ORDER BY created_at DESC 
        LIMIT 5
    '''
    recent_users = execute_all(recent_users_sql)
    for user in recent_users:
        if user.get('registration_date') and isinstance(user['registration_date'], str):
            try:
                user['registration_date'] = datetime.fromisoformat(user['registration_date'])
            except:
                pass
    
    # Recent transactions
    recent_transactions_sql = '''
        SELECT t.id, t.amount, t.type, t.created_at as timestamp, u.name as username 
        FROM "transaction" t
        LEFT JOIN "user" u ON t.user_id = u.id
        ORDER BY t.created_at DESC 
        LIMIT 5
    '''
    recent_transactions = execute_all(recent_transactions_sql)
    for txn in recent_transactions:
        if txn.get('timestamp') and isinstance(txn['timestamp'], str):
            try:
                txn['timestamp'] = datetime.fromisoformat(txn['timestamp'])
            except:
                pass
        if not txn.get('username'):
            txn['username'] = 'Unknown'
    
    # Pending withdrawals
    pending_withdrawals_sql = '''
        SELECT w.id, w.amount, w.created_at, u.name as username 
        FROM withdrawal w
        LEFT JOIN "user" u ON w.user_id = u.id
        WHERE w.status = 'pending'
        ORDER BY w.created_at DESC 
        LIMIT 5
    '''
    pending_withdrawals = execute_all(pending_withdrawals_sql)
    for withdrawal in pending_withdrawals:
        if withdrawal.get('created_at') and isinstance(withdrawal['created_at'], str):
            try:
                withdrawal['created_at'] = datetime.fromisoformat(withdrawal['created_at'])
            except:
                pass
        if not withdrawal.get('username'):
            withdrawal['username'] = 'Unknown'
    
    # Active packages
    active_packages_sql = '''
        SELECT id, name, price, daily_return_rate, duration_days, is_active
        FROM package 
        WHERE is_active = true
        ORDER BY created_at DESC 
        LIMIT 5
    '''
    active_packages = execute_all(active_packages_sql)
    
    return render_template('admin/dashboard.html', 
                         stats=stats, 
                         recent_users=recent_users,
                         recent_transactions=recent_transactions,
                         pending_withdrawals=pending_withdrawals,
                         active_packages=active_packages,
                         admin=get_current_admin())

@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Admin login"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        admin_sql = 'SELECT * FROM admin_users WHERE username = :username AND is_active = true'
        admin = execute_one(admin_sql, {'username': username})
        
        if admin and check_password_hash(admin.get('password_hash', ''), password):
            session['admin_id'] = admin['id']
            
            # Update last login
            update_sql = 'UPDATE admin_users SET last_login = :now WHERE id = :id'
            execute_update(update_sql, {'now': datetime.now(), 'id': admin['id']})
            
            flash(f'Welcome back, {admin.get("username")}!', 'success')
            return redirect(url_for('admin.dashboard'))
        else:
            flash('Invalid username or password.', 'error')
    
    return render_template('admin/login.html')

@admin_bp.route('/logout')
def logout():
    """Admin logout"""
    session.pop('admin_id', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('admin.login'))

@admin_bp.route('/users')
@require_admin_login
def users():
    """Manage users"""
    from sqlalchemy import text
    
    # Get search and filter parameters
    search = request.args.get('search', '')
    page = int(request.args.get('page', 1))
    try:
        per_page = int(request.args.get('per_page', 20))
    except (TypeError, ValueError):
        per_page = 20
    per_page = max(1, min(per_page, 200))
    offset = (page - 1) * per_page
    
    # Build query
    where_clause = ""
    params = {}
    
    if search:
        where_clause = "WHERE name ILIKE :search OR email ILIKE :search"
        params['search'] = f'%{search}%'
    
    # Get users
    query = f'''
        SELECT id, name as username, email, phone, created_at as registration_date, is_active, 
               (COALESCE(recharge_balance, 0) + COALESCE(main_balance, 0)) as balance, referred_by
        FROM "user" 
        {where_clause}
        ORDER BY created_at DESC 
        LIMIT :per_page OFFSET :offset
    '''
    params['per_page'] = per_page
    params['offset'] = offset
    
    users_raw = execute_all(query, params)
    
    # Convert to list of dicts for template
    users_list = []
    for user in users_raw:
        users_list.append({
            'id': user.get('id'),
            'username': user.get('username'),
            'email': user.get('email'),
            'balance': user.get('balance') or 0,
            'is_active': bool(user.get('is_active')),
            'registration_date': user.get('registration_date')
        })
    
    # Get total counts
    count_query = f'SELECT COUNT(*) as count FROM "user" {where_clause}'
    total_users = execute_scalar(f'SELECT COUNT(*) FROM "user" {where_clause}', params if search else {}) or 0
    
    # Active users total
    total_active_users = execute_scalar('SELECT COUNT(*) FROM "user" WHERE is_active = true') or 0
    
    # Registered today total
    registered_today_count = execute_scalar("SELECT COUNT(*) FROM \"user\" WHERE DATE(created_at) = CURRENT_DATE") or 0
    
    # Calculate pagination
    total_pages = max(1, (total_users + per_page - 1) // per_page)
    
    return render_template('admin/users.html', 
                         users=users_list,
                         current_page=page,
                         total_pages=total_pages,
                         total_users=total_users,
                         total_active_users=total_active_users,
                         registered_today_count=registered_today_count,
                         per_page=per_page,
                         search=search,
                         admin=get_current_admin())

@admin_bp.route('/users/<int:user_id>/impersonate')
@require_admin_login
def impersonate_user(user_id):
    """Impersonate (login as) a user from the admin panel."""
    conn = get_db_connection()
    try:
        user = conn.execute('SELECT id, name, email, is_active FROM user WHERE id = ?', (user_id,)).fetchone()
        if not user:
            flash('User not found.', 'error')
            return redirect(url_for('admin.users'))
        if user['is_active'] == 0:
            flash('Cannot impersonate an inactive user.', 'error')
            return redirect(url_for('admin.users'))

        # Begin impersonation
        start_impersonation(user)
        flash(f"Now impersonating {user['name']} ({user['email']}).", 'info')
        return redirect(url_for('dashboard'))
    except Exception as e:
        flash(f'Error starting impersonation: {str(e)}', 'error')
        return redirect(url_for('admin.users'))
    finally:
        conn.close()

@admin_bp.route('/stop-impersonation')
@require_admin_login
def stop_impersonation():
    """End impersonation and return to admin panel."""
    stop_impersonation_session()
    flash('Impersonation ended. Returned to admin panel.', 'info')
    # Prefer returning to users list for context
    return redirect(url_for('admin.users'))

@admin_bp.route('/users/<int:user_id>/add-balance', methods=['GET', 'POST'])
@require_admin_login
def add_user_balance(user_id):
    """Add balance to user account"""
    conn = get_db_connection()
    
    # Get user details
    user = conn.execute('SELECT * FROM user WHERE id = ?', (user_id,)).fetchone()
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('admin.users'))
    
    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount'))
            balance_type = request.form.get('balance_type')  # 'recharge' or 'main'
            description = request.form.get('description', 'Admin credit')
            
            if amount <= 0:
                flash('Amount must be greater than 0.', 'error')
                return render_template('admin/add_user_balance.html', user=user, admin=get_current_admin())
            
            # Update user balance
            if balance_type == 'recharge':
                current_balance = user['recharge_balance'] or 0
                new_balance = current_balance + amount
                conn.execute('UPDATE user SET recharge_balance = ? WHERE id = ?', 
                           (new_balance, user_id))
                balance_column = 'recharge_balance'
            else:  # main balance
                current_balance = user['main_balance'] or 0
                new_balance = current_balance + amount
                conn.execute('UPDATE user SET main_balance = ? WHERE id = ?', 
                           (new_balance, user_id))
                balance_column = 'main_balance'
            
            # Create transaction record
            conn.execute('''
                INSERT INTO "transaction" (user_id, type, amount, description, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, 'admin_credit', amount, description, datetime.now()))
            
            conn.commit()
            conn.close()
            
            flash(f'Successfully added ₦{amount:,.2f} to {user["name"]}\'s {balance_type} balance.', 'success')
            return redirect(url_for('admin.users'))
            
        except ValueError:
            flash('Invalid amount entered.', 'error')
        except Exception as e:
            flash(f'Error adding balance: {str(e)}', 'error')
    
    conn.close()
    return render_template('admin/add_user_balance.html', user=user, admin=get_current_admin())

@admin_bp.route('/users/delete-all', methods=['POST'])
@require_admin_login
def delete_all_users():
    """Delete all users and related records (transactions, withdrawals, packages)."""
    # End any active impersonation/user session to avoid stale session refs
    stop_impersonation_session()

    conn = get_db_connection()
    try:
        # Use a transaction for safety
        conn.execute('BEGIN')
        # Delete child records first to satisfy FK constraints
        conn.execute('DELETE FROM "transaction"')
        conn.execute('DELETE FROM withdrawal')
        conn.execute('DELETE FROM user_package')
        # Finally delete users
        conn.execute('DELETE FROM user')
        conn.commit()
        flash('All users and their related records have been deleted.', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Failed to delete users: {str(e)}', 'error')
    finally:
        conn.close()
    return redirect(url_for('admin.users'))

@admin_bp.route('/packages')
@require_admin_login
def packages():
    """Manage packages"""
    conn = get_db_connection()
    
    packages_raw = conn.execute('''
        SELECT id, name, description, price, daily_return_rate, duration_days, is_active, created_at
        FROM package 
        ORDER BY price ASC
    ''').fetchall()
    
    # Convert to list of dicts for template
    packages_list = []
    for package in packages_raw:
        created_at = package['created_at']
        if created_at and isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        packages_list.append({
            'id': package['id'],
            'name': package['name'],
            'description': package['description'],
            'price': package['price'],
            'daily_return': package['daily_return_rate'],
            'duration': package['duration_days'],
            'is_active': bool(package['is_active']),
            'created_at': created_at
        })
    
    conn.close()
    
    return render_template('admin/packages.html', 
                         packages=packages_list,
                         admin=get_current_admin())

@admin_bp.route('/transactions')
@require_admin_login
def transactions():
    """View transactions"""
    conn = get_db_connection()
    
    # Get filters
    transaction_type = request.args.get('type', '')
    page = int(request.args.get('page', 1))
    per_page = 50
    offset = (page - 1) * per_page
    
    where_clause = ""
    params = []
    
    if transaction_type:
        where_clause = "WHERE t.type = ?"
        params = [transaction_type]
    
    # Get transactions
    query = f'''
        SELECT t.id, t.amount, t.type, t.created_at, t.description, u.name as username 
        FROM "transaction" t
        LEFT JOIN "user" u ON t.user_id = u.id
        {where_clause}
        ORDER BY t.created_at DESC 
        LIMIT ? OFFSET ?
    '''
    params.extend([per_page, offset])
    
    transactions_raw = conn.execute(query, params).fetchall()
    
    # Convert to list of dicts for template
    transactions_list = []
    for txn in transactions_raw:
        user_obj = type('User', (), {'username': txn['username'] if txn['username'] else 'Unknown'})()
        created_at = txn['created_at']
        if created_at and isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        transactions_list.append({
            'id': txn['id'],
            'amount': txn['amount'],
            'type': txn['type'],
            'timestamp': created_at,
            'description': txn['description'],
            'user': user_obj
        })
    
    # Get total count
    count_query = f'SELECT COUNT(*) as count FROM "transaction" t {where_clause}'
    count_params = params[:-2] if transaction_type else []
    total_transactions = conn.execute(count_query, count_params).fetchone()['count']
    
    # Calculate total volume
    volume_query = f'SELECT COALESCE(SUM(amount), 0) as total FROM "transaction" t {where_clause}'
    total_volume = conn.execute(volume_query, count_params).fetchone()['total']
    
    # Calculate today's volume
    today_query = f'''
        SELECT COALESCE(SUM(amount), 0) as total 
        FROM "transaction" t 
        WHERE DATE(t.created_at) = DATE('now')
        {" AND " + where_clause.replace("WHERE ", "") if where_clause else ""}
    '''
    today_params = count_params if where_clause else []
    today_volume = conn.execute(today_query, today_params).fetchone()['total']
    
    conn.close()
    
    # Calculate pagination
    total_pages = (total_transactions + per_page - 1) // per_page
    
    return render_template('admin/transactions.html', 
                         transactions=transactions_list,
                         current_page=page,
                         total_pages=total_pages,
                         transaction_type=transaction_type,
                         total_volume=total_volume,
                         today_volume=today_volume,
                         now=datetime.now(),
                         admin=get_current_admin())

@admin_bp.route('/withdrawals')
@require_admin_login
def withdrawals():
    """Manage withdrawal requests"""
    conn = get_db_connection()
    
    # Get withdrawal requests from the withdrawal table
    try:
        withdrawals_raw = conn.execute('''
            SELECT w.id, w.amount, w.fee, w.net_amount, w.status, w.created_at, 
                   w.bank_name, w.other_bank, w.account_number, w.account_name,
                   u.name as username, u.email
            FROM withdrawal w
            LEFT JOIN "user" u ON w.user_id = u.id
            ORDER BY w.created_at DESC
        ''').fetchall()
        
        # Convert to list of dicts for template
        withdrawals_list = []
        for withdrawal in withdrawals_raw:
            user_obj = type('User', (), {'username': withdrawal['username'] if withdrawal['username'] else 'Unknown'})()
            created_at = withdrawal['created_at']
            if created_at and isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at)
            withdrawals_list.append({
                'id': withdrawal['id'],
                'amount': withdrawal['amount'],
                'fee': withdrawal['fee'],
                'net_amount': withdrawal['net_amount'],
                'status': withdrawal['status'],
                'created_at': created_at,
                'bank_name': withdrawal['bank_name'],
                'other_bank': withdrawal['other_bank'],
                'account_number': withdrawal['account_number'],
                'account_name': withdrawal['account_name'],
                'user': user_obj
            })
        
        # Calculate statistics
        total_withdrawals = sum(w['net_amount'] for w in withdrawals_list if w['status'] == 'completed')
        pending_count = sum(1 for w in withdrawals_list if w['status'] == 'pending')
        pending_amount = sum(w['amount'] for w in withdrawals_list if w['status'] == 'pending')
        
        # Today's withdrawals
        today = datetime.now().date()
        today_withdrawals = sum(
            w['net_amount'] for w in withdrawals_list 
            if w['status'] == 'completed' 
            and w['created_at'] 
            and w['created_at'].date() == today
        )
        
    except Exception as e:
        # If there's an error, show empty list
        print(f"Error fetching withdrawals: {e}")
        withdrawals_list = []
        total_withdrawals = 0
        pending_count = 0
        pending_amount = 0
        today_withdrawals = 0
    
    conn.close()
    
    return render_template('admin/withdrawals.html', 
                         withdrawals=withdrawals_list,
                         total_withdrawals=total_withdrawals,
                         pending_count=pending_count,
                         pending_amount=pending_amount,
                         today_withdrawals=today_withdrawals,
                         admin=get_current_admin())

@admin_bp.route('/gateway-withdrawal', methods=['GET', 'POST'])
@require_admin_login
def gateway_withdrawal():
    """Create a direct withdrawal request through the gateway."""
    transfer_limits = {
        'min_amount': gateway_service.transfer_min_amount,
        'max_amount': gateway_service.transfer_max_amount,
    }
    transfer_result = None
    suggested_transfer_id = f"GW{datetime.now().strftime('%m%d%H%M%S')}"
    default_apply_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if request.method == 'POST':
        amount_raw = (request.form.get('amount') or '').strip()
        transfer_id = (request.form.get('transfer_id') or suggested_transfer_id).strip()
        bank_code = (request.form.get('bank_code') or '').strip()
        receive_name = (request.form.get('receive_name') or '').strip()
        receive_account = (request.form.get('receive_account') or '').strip()
        number = (request.form.get('number') or '').strip() or None
        email = (request.form.get('email') or '').strip() or None
        mobile = (request.form.get('mobile') or '').strip() or None
        remark = (request.form.get('remark') or '').strip() or None
        back_url = (request.form.get('back_url') or '').strip() or None
        apply_date = (request.form.get('apply_date') or '').strip() or None

        if not amount_raw or not bank_code or not receive_name or not receive_account:
            flash('Amount, bank code, recipient name, and recipient account are required.', 'danger')
        else:
            try:
                transfer_result = gateway_service.create_transfer_payment(
                    amount=float(amount_raw),
                    transfer_id=transfer_id,
                    bank_code=bank_code,
                    receive_name=receive_name,
                    receive_account=receive_account,
                    number=number,
                    email=email,
                    mobile=mobile,
                    remark=remark,
                    back_url=back_url,
                    apply_date=apply_date,
                )
                if transfer_result.get('success'):
                    flash(transfer_result.get('message') or 'Transfer request submitted successfully.', 'success')
                else:
                    flash(transfer_result.get('message') or 'Transfer request failed.', 'danger')
            except ValueError:
                flash('Amount must be a valid number.', 'danger')

        suggested_transfer_id = transfer_id

    return render_template(
        'admin/gateway_withdrawal.html',
        admin=get_current_admin(),
        transfer_result=transfer_result,
        transfer_limits=transfer_limits,
        suggested_transfer_id=suggested_transfer_id,
        default_bank_code=gateway_service.bank_code,
        default_apply_date=default_apply_date,
        # Provide a friendly list of bank names for the admin dropdown.
        # NOTE: by default these options submit the configured gateway bank code
        # (NEKPAY_BANK_CODE). You can later map specific provider codes if needed.
        bank_options=[
            ('NGR044', 'Access Bank'),
            ('NGR050', 'Ecobank Nigeria'),
            ('NGR000019', 'Enterprise Bank'),
            ('NGR070', 'Fidelity Bank'),
            ('NGR011', 'First Bank of Nigeria'),
            ('NGR214', 'First City Monument Bank'),
            ('NGR00103', 'Globus Bank'),
            ('NGR058', 'Guaranty Trust Bank'),
            ('NGR301', 'Jaiz Bank'),
            ('NGR082', 'Keystone Bank'),
            ('NGR50211', 'Kuda Bank'),
            ('NGR565', 'One Finance'),
            ('NGR999991', 'PalmPay'),
            ('NGR526', 'Parallex Bank'),
            ('NGR076', 'Polaris Bank'),
            ('NGR101', 'Providus Bank'),
            ('NGR221', 'Stanbic IBTC Bank'),
            ('NGR068', 'Standard Chartered Bank'),
            ('NGR232', 'Sterling Bank'),
            ('NGR100', 'Suntrust Bank'),
            ('NGR51310', 'Sparkle Microfinance Bank'),
            ('NGR302', 'TAJ Bank'),
            ('NGR032', 'Union Bank of Nigeria'),
            ('NGR033', 'United Bank For Africa'),
            ('NGR215', 'Unity Bank'),
            ('NGR566', 'VFD Microfinance Bank'),
            ('NGR035', 'Wema Bank'),
            ('NGR057', 'Zenith Bank'),
            ('NGR801', 'Abbey Mortgage Bank'),
            ('NGR035A', 'ALAT by WEMA'),
            ('NGR999992', 'Paycom'),
            ('NGR20009', 'Opay'),
            ('NGR100031', 'FCMB Easy Account'),
            ('NGR999993', 'Moniepoint MFB'),
            ('NGR031', 'Premium Trust Bank'),
            ('NG0206', 'Paga'),
        ],
    )

# API Routes
@admin_bp.route('/api/user/<int:user_id>/toggle-status', methods=['POST'])
@require_admin_login
def toggle_user_status(user_id):
    """Toggle user active status"""
    conn = get_db_connection()
    
    user = conn.execute('SELECT * FROM user WHERE id = ?', (user_id,)).fetchone()
    if user:
        new_status = 0 if user['is_active'] else 1
        conn.execute('UPDATE user SET is_active = ? WHERE id = ?', (new_status, user_id))
        conn.commit()
        
        status_text = 'activated' if new_status else 'deactivated'
        return jsonify({'success': True, 'message': f'User {status_text} successfully.'})
    
    conn.close()
    return jsonify({'success': False, 'message': 'User not found.'})

@admin_bp.route('/api/package/<int:package_id>/toggle-status', methods=['POST'])
@require_admin_login
def toggle_package_status(package_id):
    """Toggle package active status"""
    conn = get_db_connection()
    
    package = conn.execute('SELECT * FROM package WHERE id = ?', (package_id,)).fetchone()
    if package:
        new_status = 0 if package['is_active'] else 1
        conn.execute('UPDATE package SET is_active = ? WHERE id = ?', (new_status, package_id))
        conn.commit()
        
        status_text = 'activated' if new_status else 'deactivated'
        return jsonify({'success': True, 'message': f'Package {status_text} successfully.'})
    
    conn.close()
    return jsonify({'success': False, 'message': 'Package not found.'})

@admin_bp.route('/api/user/<int:user_id>/details', methods=['GET'])
@require_admin_login
def get_user_details(user_id):
    """Get detailed user information including packages"""
    conn = get_db_connection()
    
    try:
        # Get user info
        user = conn.execute('''
            SELECT id, name as username, email, phone, 
                   (COALESCE(recharge_balance, 0) + COALESCE(main_balance, 0)) as balance,
                   recharge_balance, main_balance, is_active, created_at, referred_by
            FROM "user" 
            WHERE id = ?
        ''', (user_id,)).fetchone()
        
        if not user:
            return jsonify({'success': False, 'message': 'User not found.'})
        
        # Get user packages
        packages = conn.execute('''
            SELECT up.id, up.amount_invested, up.daily_return, up.total_earned,
                   up.start_date, up.end_date, up.is_active, up.last_payout,
                   p.name as package_name, p.duration_days
            FROM user_package up
            JOIN package p ON up.package_id = p.id
            WHERE up.user_id = ?
            ORDER BY up.start_date DESC
        ''', (user_id,)).fetchall()
        
        # Convert packages to list of dicts
        packages_list = []
        for pkg in packages:
            packages_list.append({
                'id': pkg['id'],
                'package_name': pkg['package_name'],
                'amount_invested': pkg['amount_invested'],
                'daily_return': pkg['daily_return'],
                'total_earned': pkg['total_earned'],
                'start_date': pkg['start_date'],
                'end_date': pkg['end_date'],
                'is_active': bool(pkg['is_active']),
                'last_payout': pkg['last_payout'],
                'duration_days': pkg['duration_days'],
                'status': 'Active' if pkg['is_active'] else 'Inactive'
            })
        
        user_data = {
            'id': user['id'],
            'username': user['username'],
            'email': user['email'],
            'phone': user['phone'],
            'balance': user['balance'],
            'recharge_balance': user['recharge_balance'],
            'main_balance': user['main_balance'],
            'is_active': bool(user['is_active']),
            'created_at': user['created_at'],
            'packages': packages_list
        }
        
        return jsonify({'success': True, 'user': user_data})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@admin_bp.route('/api/user-package/<int:package_id>/toggle-status', methods=['POST'])
@require_admin_login
def toggle_user_package_status(package_id):
    """Toggle user package active status (activate/deactivate)"""
    conn = get_db_connection()
    
    try:
        # Get the user package
        user_package = conn.execute('''
            SELECT up.*, u.name as username, p.name as package_name
            FROM user_package up
            JOIN "user" u ON up.user_id = u.id
            JOIN package p ON up.package_id = p.id
            WHERE up.id = ?
        ''', (package_id,)).fetchone()
        
        if not user_package:
            return jsonify({'success': False, 'message': 'User package not found.'})
        
        # Toggle status - use boolean values for PostgreSQL
        new_status = not bool(user_package['is_active'])
        conn.execute('UPDATE user_package SET is_active = ? WHERE id = ?', (new_status, package_id))
        conn.commit()
        
        status_text = 'activated' if new_status else 'deactivated'
        
        return jsonify({
            'success': True, 
            'message': f'Package {status_text} successfully for {user_package["username"]}.',
            'new_status': new_status
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@admin_bp.route('/api/withdrawal/<int:withdrawal_id>/approve', methods=['POST'])
@require_admin_login
def approve_withdrawal(withdrawal_id):
    """Approve withdrawal request"""
    conn = get_db_connection()
    
    try:
        # Get withdrawal request
        withdrawal = conn.execute('''
            SELECT w.*, u.name as username, u.email as user_email
            FROM withdrawal w
            LEFT JOIN "user" u ON w.user_id = u.id
            WHERE w.id = ? AND w.status = 'pending'
        ''', (withdrawal_id,)).fetchone()
        
        if not withdrawal:
            return jsonify({'success': False, 'message': 'Withdrawal request not found or already processed.'})
        
        # Update withdrawal status to completed
        conn.execute('''
            UPDATE withdrawal 
            SET status = 'completed', processed_at = datetime('now'), processed_by = ?
            WHERE id = ?
        ''', (get_current_admin()['id'], withdrawal_id,))
        
        conn.commit()

        if withdrawal.get('user_email'):
            _send_admin_mail_async(
                subject='Withdrawal approved - Funds on the way',
                recipient=withdrawal['user_email'],
                html_template='emails/withdrawal_approved.html',
                text_template='emails/withdrawal_approved.txt',
                context={
                    'name': withdrawal.get('username') or 'User',
                    'email': withdrawal.get('user_email'),
                    'amount': f"{(withdrawal.get('amount') or 0):,.0f}",
                    'net_amount': f"{(withdrawal.get('net_amount') or 0):,.0f}",
                    'bank_name': withdrawal.get('bank_name') or 'N/A',
                    'account_number': withdrawal.get('account_number') or 'N/A',
                    'approval_date': datetime.now().strftime('%Y-%m-%d %H:%M UTC'),
                    'trace_id': f"WD-{withdrawal_id}",
                    'dashboard_url': url_for('dashboard', _external=True),
                },
                success_log='Withdrawal approved email',
                error_log='withdrawal approved email',
            )

        conn.close()
        
        return jsonify({'success': True, 'message': 'Withdrawal approved successfully.'})
        
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': f'Error approving withdrawal: {str(e)}'})

@admin_bp.route('/api/withdrawal/<int:withdrawal_id>/reject', methods=['POST'])
@require_admin_login
def reject_withdrawal(withdrawal_id):
    """Reject withdrawal request"""
    conn = get_db_connection()
    
    try:
        data = request.get_json() if request.is_json else {}
        reason = data.get('reason', 'Rejected by admin')
        
        # Get withdrawal request
        withdrawal = conn.execute('''
            SELECT w.*, u.name as username, u.email as user_email
            FROM withdrawal w
            LEFT JOIN "user" u ON w.user_id = u.id
            WHERE w.id = ? AND w.status = 'pending'
        ''', (withdrawal_id,)).fetchone()
        
        if not withdrawal:
            return jsonify({'success': False, 'message': 'Withdrawal request not found or already processed.'})
        
        # Refund amount to user's main balance (deducted at request time)
        conn.execute('''
            UPDATE user 
            SET main_balance = main_balance + ? 
            WHERE id = ?
        ''', (withdrawal['amount'], withdrawal['user_id']))
        
        # Update withdrawal status to rejected
        conn.execute('''
            UPDATE withdrawal 
            SET status = 'rejected', reason = ?, processed_at = datetime('now'), processed_by = ?
            WHERE id = ?
        ''', (f"Rejected: {reason}", get_current_admin()['id'], withdrawal_id))
        
        conn.commit()

        if withdrawal.get('user_email'):
            _send_admin_mail_async(
                subject='Withdrawal declined - Refunded to your account',
                recipient=withdrawal['user_email'],
                html_template='emails/withdrawal_declined.html',
                text_template='emails/withdrawal_declined.txt',
                context={
                    'name': withdrawal.get('username') or 'User',
                    'email': withdrawal.get('user_email'),
                    'amount': f"{(withdrawal.get('amount') or 0):,.0f}",
                    'decline_date': datetime.now().strftime('%Y-%m-%d %H:%M UTC'),
                    'decline_reason': reason,
                    'dashboard_url': url_for('dashboard', _external=True),
                },
                success_log='Withdrawal declined email',
                error_log='withdrawal declined email',
            )

        conn.close()
        
        return jsonify({'success': True, 'message': 'Withdrawal rejected and amount refunded to user.'})
        
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': f'Error rejecting withdrawal: {str(e)}'})

@admin_bp.route('/packages/new', methods=['GET', 'POST'])
@require_admin_login
def new_package():
    """Create new package"""
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        
        # Validate required fields
        name = data.get('name', '').strip()
        description = data.get('description', '').strip()
        price = data.get('price')
        daily_return_rate = data.get('daily_return_rate')
        duration_days = data.get('duration_days')
        is_active = data.get('is_active')
        
        # Validation
        errors = []
        if not name:
            errors.append('Package name is required')
        if not description:
            errors.append('Package description is required')
        
        try:
            price = float(price) if price else 0
            if price <= 0:
                errors.append('Price must be greater than 0')
        except (ValueError, TypeError):
            errors.append('Invalid price format')
            price = 0
        
        try:
            daily_return_rate = float(daily_return_rate) if daily_return_rate else 0
            if daily_return_rate <= 0 or daily_return_rate > 100:
                errors.append('Daily return rate must be between 0.01 and 100')
        except (ValueError, TypeError):
            errors.append('Invalid daily return rate format')
            daily_return_rate = 0
        
        try:
            duration_days = int(duration_days) if duration_days else 0
            if duration_days <= 0:
                errors.append('Duration must be greater than 0 days')
        except (ValueError, TypeError):
            errors.append('Invalid duration format')
            duration_days = 0

        is_active = True if str(is_active).lower() in ('1', 'true', 'on', 'yes') else False
        
        if errors:
            if request.is_json:
                return jsonify({'success': False, 'errors': errors})
            else:
                for error in errors:
                    flash(error, 'error')
                return render_template('admin/new_package.html', 
                                     admin=get_current_admin(),
                                     form_data=data)
        
        # Create package
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO package (name, description, price, daily_return_rate, duration_days, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ''', (name, description, price, daily_return_rate, duration_days, is_active))
            
            package_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            if request.is_json:
                return jsonify({'success': True, 'message': 'Package created successfully', 'package_id': package_id})
            else:
                flash('Package created successfully!', 'success')
                return redirect(url_for('admin.packages'))
                
        except Exception as e:
            conn.rollback()
            conn.close()
            error_msg = f'Error creating package: {str(e)}'
            if request.is_json:
                return jsonify({'success': False, 'message': error_msg})
            else:
                flash(error_msg, 'error')
                return render_template('admin/new_package.html', 
                                     admin=get_current_admin(),
                                     form_data=data)
    
    # GET request - show form
    return render_template('admin/new_package.html', admin=get_current_admin())

@admin_bp.route('/packages/<int:package_id>/edit', methods=['GET', 'POST'])
@require_admin_login
def edit_package(package_id):
    """Edit existing package"""
    conn = get_db_connection()
    
    # Get package
    package = conn.execute('SELECT * FROM package WHERE id = ?', (package_id,)).fetchone()
    if not package:
        conn.close()
        flash('Package not found', 'error')
        return redirect(url_for('admin.packages'))
    
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        
        # Validate required fields
        name = data.get('name', '').strip()
        description = data.get('description', '').strip()
        price = data.get('price')
        daily_return_rate = data.get('daily_return_rate')
        duration_days = data.get('duration_days')
        
        # Validation
        errors = []
        if not name:
            errors.append('Package name is required')
        if not description:
            errors.append('Package description is required')
        
        try:
            price = float(price) if price else 0
            if price <= 0:
                errors.append('Price must be greater than 0')
        except (ValueError, TypeError):
            errors.append('Invalid price format')
            price = 0
        
        try:
            daily_return_rate = float(daily_return_rate) if daily_return_rate else 0
            if daily_return_rate <= 0 or daily_return_rate > 100:
                errors.append('Daily return rate must be between 0.01 and 100')
        except (ValueError, TypeError):
            errors.append('Invalid daily return rate format')
            daily_return_rate = 0
        
        try:
            duration_days = int(duration_days) if duration_days else 0
            if duration_days <= 0:
                errors.append('Duration must be greater than 0 days')
        except (ValueError, TypeError):
            errors.append('Invalid duration format')
            duration_days = 0
        
        if errors:
            if request.is_json:
                return jsonify({'success': False, 'errors': errors})
            else:
                for error in errors:
                    flash(error, 'error')
                return render_template('admin/edit_package.html', 
                                     admin=get_current_admin(),
                                     package=package,
                                     form_data=data)
        
        # Update package
        try:
            conn.execute('''
                UPDATE package 
                SET name = ?, description = ?, price = ?, daily_return_rate = ?, duration_days = ?
                WHERE id = ?
            ''', (name, description, price, daily_return_rate, duration_days, package_id))
            
            conn.commit()
            conn.close()
            
            if request.is_json:
                return jsonify({'success': True, 'message': 'Package updated successfully'})
            else:
                flash('Package updated successfully!', 'success')
                return redirect(url_for('admin.packages'))
                
        except Exception as e:
            conn.rollback()
            conn.close()
            error_msg = f'Error updating package: {str(e)}'
            if request.is_json:
                return jsonify({'success': False, 'message': error_msg})
            else:
                flash(error_msg, 'error')
                return render_template('admin/edit_package.html', 
                                     admin=get_current_admin(),
                                     package=package,
                                     form_data=data)
    
    # GET request - show form
    conn.close()
    return render_template('admin/edit_package.html', 
                         admin=get_current_admin(),
                         package=package)

@admin_bp.route('/api/package/<int:package_id>/delete', methods=['POST', 'DELETE'])
@require_admin_login
def delete_package(package_id):
    """Delete package (soft delete by deactivating)"""
    conn = get_db_connection()
    
    package = conn.execute('SELECT * FROM package WHERE id = ?', (package_id,)).fetchone()
    if package:
        # Check if package is in use
        users_with_package = conn.execute('''
            SELECT COUNT(*) as count FROM user_package 
            WHERE package_id = ? AND is_active = 1
        ''', (package_id,)).fetchone()
        
        if users_with_package and users_with_package['count'] > 0:
            conn.close()
            return jsonify({'success': False, 'message': 'Cannot delete package that is currently in use by users.'})
        
        # Soft delete by deactivating
        conn.execute('UPDATE package SET is_active = 0 WHERE id = ?', (package_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Package deleted successfully.'})
    
    conn.close()
    return jsonify({'success': False, 'message': 'Package not found.'})

@admin_bp.route('/deposits/pending')
@require_admin_login
def pending_deposits():
    """View all pending deposit requests"""
    conn = get_db_connection()
    
    # Get all pending deposits with user information
    pending_deposits = conn.execute('''
        SELECT t.*, u.name, u.email, u.phone
        FROM "transaction" t
        JOIN "user" u ON t.user_id = u.id
        WHERE t.type = 'deposit' AND t.status = 'pending'
        ORDER BY t.created_at DESC
    ''').fetchall()
    
    conn.close()
    
    return render_template('admin/pending_deposits.html', 
                         deposits=pending_deposits,
                         admin=get_current_admin())

@admin_bp.route('/deposits/approve/<int:transaction_id>', methods=['POST'])
@require_admin_login
def approve_deposit(transaction_id):
    """Approve a pending deposit"""
    conn = None
    
    try:
        conn = get_db_connection()
        
        # Get amount from request
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'Invalid request data'})
        
        amount = data.get('amount')
        
        if not amount:
            return jsonify({'success': False, 'message': 'Amount is required'})
        
        try:
            amount = float(amount)
            if amount <= 0:
                return jsonify({'success': False, 'message': 'Amount must be greater than 0'})
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'Invalid amount format'})
        
        # Get transaction details
        transaction = conn.execute(
            'SELECT * FROM "transaction" WHERE id = ? AND type = ? AND status = ?',
            (transaction_id, 'deposit', 'pending')
        ).fetchone()
        
        if not transaction:
            return jsonify({'success': False, 'message': 'Transaction not found or already processed'})
        
        # Verify user exists
        user = conn.execute(
            'SELECT id, recharge_balance FROM "user" WHERE id = ?',
            (transaction['user_id'],)
        ).fetchone()
        
        if not user:
            return jsonify({'success': False, 'message': 'User not found'})
        
        # Update transaction status and amount
        conn.execute('''
            UPDATE "transaction" 
            SET status = 'completed',
                amount = ?,
                description = 'Manual deposit - Approved by admin'
            WHERE id = ?
        ''', (amount, transaction_id))
        
        # Add amount to user recharge_balance (deposits go to recharge balance)
        conn.execute('''
            UPDATE "user" 
            SET recharge_balance = recharge_balance + ?
            WHERE id = ?
        ''', (amount, transaction['user_id']))
        
        conn.commit()
        
        return jsonify({'success': True, 'message': f'Deposit of ₦{amount:,.0f} approved successfully'})
        
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except:
                pass
        error_msg = str(e)
        print(f"Error approving deposit {transaction_id}: {error_msg}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'Failed to approve deposit: {error_msg}'})
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

@admin_bp.route('/deposits/reject/<int:transaction_id>', methods=['POST'])
@require_admin_login
def reject_deposit(transaction_id):
    """Reject a pending deposit"""
    conn = None
    
    try:
        conn = get_db_connection()
        
        # Get rejection reason
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'Invalid request data'})
        
        reason = data.get('reason', 'No reason provided')
        if not reason or not reason.strip():
            reason = 'No reason provided'
        
        # Get transaction details
        transaction = conn.execute(
            'SELECT * FROM "transaction" WHERE id = ? AND type = ? AND status = ?',
            (transaction_id, 'deposit', 'pending')
        ).fetchone()
        
        if not transaction:
            return jsonify({'success': False, 'message': 'Transaction not found or already processed'})
        
        # Update transaction status
        conn.execute('''
            UPDATE "transaction" 
            SET status = 'failed',
                description = ?
            WHERE id = ?
        ''', (f'Rejected by admin: {reason}', transaction_id))
        
        conn.commit()
        
        return jsonify({'success': True, 'message': 'Deposit rejected successfully'})
        
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except:
                pass
        error_msg = str(e)
        print(f"Error rejecting deposit {transaction_id}: {error_msg}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'Failed to reject deposit: {error_msg}'})
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

@admin_bp.route('/user-deposits')
@require_admin_login
def user_deposits():
    """View all user deposits (not admin-added credits)"""
    conn = get_db_connection()
    
    try:
        # Get all user deposits (type='deposit' only, excludes admin_credit)
        # with pagination for better performance
        page = request.args.get('page', 1, type=int)
        per_page = 50
        offset = (page - 1) * per_page
        
        # Get total count
        total = conn.execute('''
            SELECT COUNT(*) as count
            FROM "transaction" t
            WHERE t.type = 'deposit'
        ''').fetchone()
        total_count = total['count'] if total else 0
        total_pages = (total_count + per_page - 1) // per_page
        
        # Get paginated deposits with user info
        deposits = conn.execute('''
            SELECT t.id, t.user_id, t.amount, t.status, t.created_at, t.description, t.reference,
                   u.name, u.email, u.phone
            FROM "transaction" t
            JOIN "user" u ON t.user_id = u.id
            WHERE t.type = 'deposit'
            ORDER BY t.created_at DESC
            LIMIT ? OFFSET ?
        ''', (per_page, offset)).fetchall()
        
        # Calculate statistics
        stats = conn.execute('''
            SELECT 
                COUNT(*) as total_deposits,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_count,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                SUM(CASE WHEN status = 'completed' THEN amount ELSE 0 END) as total_amount
            FROM "transaction"
            WHERE type = 'deposit'
        ''').fetchone()
        
        conn.close()
        
        # Convert to dictionaries for easier template access
        deposits_list = [dict(d) for d in deposits] if deposits else []
        stats_dict = dict(stats) if stats else {}
        
        return render_template('admin/user_deposits.html',
                             deposits=deposits_list,
                             current_page=page,
                             total_pages=total_pages,
                             total_count=total_count,
                             stats=stats_dict,
                             admin=get_current_admin())
    
    except Exception as e:
        print(f"Error fetching user deposits: {str(e)}")
        import traceback
        traceback.print_exc()
        flash('Error loading deposits', 'error')
        return redirect(url_for('admin.dashboard'))
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

@admin_bp.route('/receipt/<int:transaction_id>')
@require_admin_login
def view_receipt(transaction_id):
    """View user deposit receipt"""
    try:
        conn = get_db_connection()
        
        # Get transaction with user details
        transaction = conn.execute('''
            SELECT t.*, u.name, u.email, u.phone
            FROM "transaction" t
            JOIN "user" u ON t.user_id = u.id
            WHERE t.id = ? AND t.type = 'deposit'
        ''', (transaction_id,)).fetchone()
        
        if not transaction:
            flash('Receipt not found', 'error')
            return redirect(url_for('admin.pending_deposits'))
        
        # Extract filename from description
        receipt_filename = ''
        if transaction['description'] and 'Receipt:' in transaction['description']:
            receipt_filename = transaction['description'].split('Receipt: ')[-1]
        
        conn.close()
        
        return render_template('admin/view_receipt.html',
                             transaction=dict(transaction),
                             receipt_filename=receipt_filename,
                             admin=get_current_admin())
        
    except Exception as e:
        print(f"Error viewing receipt: {str(e)}")
        flash('Error loading receipt', 'error')
        return redirect(url_for('admin.pending_deposits'))

@admin_bp.route('/api/receipt/<int:transaction_id>/data')
@require_admin_login
def get_receipt_data(transaction_id):
    """Get receipt data as JSON for modal display"""
    try:
        conn = get_db_connection()
        
        # Get transaction with user details
        transaction = conn.execute('''
            SELECT t.*, u.name, u.email, u.phone
            FROM "transaction" t
            JOIN "user" u ON t.user_id = u.id
            WHERE t.id = ? AND t.type = 'deposit'
        ''', (transaction_id,)).fetchone()
        
        if not transaction:
            return jsonify({'success': False, 'message': 'Receipt not found'}), 404
        
        # Extract filename from description
        receipt_filename = ''
        if transaction['description'] and 'Receipt:' in transaction['description']:
            receipt_filename = transaction['description'].split('Receipt: ')[-1]
        
        conn.close()
        
        return jsonify({
            'success': True,
            'transaction_id': transaction['id'],
            'user_name': transaction['name'],
            'user_email': transaction['email'],
            'user_phone': transaction['phone'],
            'amount': transaction['amount'],
            'reference': transaction['reference'],
            'status': transaction['status'],
            'created_at': transaction['created_at'],
            'receipt_filename': receipt_filename,
            'description': transaction['description']
        })
        
    except Exception as e:
        print(f"Error getting receipt data: {str(e)}")
        return jsonify({'success': False, 'message': 'Error loading receipt data'}), 500

