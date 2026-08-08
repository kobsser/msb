import os
import uuid
import asyncio
import threading
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, session
from config import SECRET_KEY
from database import (
    init_db, get_web_user, verify_web_user, create_web_user,
    get_valid_invite, use_invite, create_invite, get_all_invites,
    save_tg_account, get_tg_account, get_tg_accounts_for_user, delete_tg_account
)
from clients import send_code, sign_in, check_password, get_groups
import workers

LOOP = asyncio.new_event_loop()
def _run_loop():
    asyncio.set_event_loop(LOOP)
    LOOP.run_forever()
threading.Thread(target=_run_loop, daemon=True).start()

def run_async(coro, timeout=None):
    return asyncio.run_coroutine_threadsafe(coro, LOOP).result(timeout=timeout)

def safe_run_async(coro, timeout=None):
    try: return run_async(coro, timeout=timeout)
    except Exception as e: return f"error: {str(e)}"

def is_session_string(result):
    return isinstance(result, str) and not result.startswith("error:") and len(result) > 100

app = Flask(__name__)
app.secret_key = SECRET_KEY
init_db()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or not session.get('is_admin'): return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route("/")
def index():
    if 'user_id' in session: return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
        user = verify_web_user(username, password)
        if user:
            session['user_id'] = user['id']
            session['is_admin'] = bool(user['is_admin'])
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        flash("نام کاربری یا رمز عبور اشتباه است", "danger")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        code = request.form.get("invite_code", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
        invite = get_valid_invite(code)
        if not invite:
            flash("کد دعوت نامعتبر یا استفاده شده است", "danger")
            return redirect(url_for('register'))
            
        if get_web_user(username):
            flash("این نام کاربری قبلا ثبت شده است", "danger")
            return redirect(url_for('register'))
            
        create_web_user(username, password)
        new_user = get_web_user(username)
        use_invite(code, new_user['id'])
        
        session['user_id'] = new_user['id']
        session['is_admin'] = bool(new_user['is_admin'])
        session['username'] = new_user['username']
        return redirect(url_for('dashboard'))
        
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    accounts = get_tg_accounts_for_user(session['user_id'])
    return render_template("dashboard.html", accounts=accounts)

@app.route("/add_account", methods=["GET", "POST"])
@login_required
def add_account():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        if not phone:
            flash("شماره را وارد کنید", "danger")
            return redirect(url_for('add_account'))
            
        result = safe_run_async(send_code(phone), timeout=60)
        if result is True:
            session["pending_phone"] = phone
            return render_template("verify_code.html", phone=phone)
        flash(str(result), "danger")
    return render_template("add_account.html")

@app.route("/verify_code", methods=["POST"])
@login_required
def verify_code():
    phone = session.get("pending_phone")
    code = request.form.get("code", "").strip()
    if not phone: return redirect(url_for('add_account'))
    
    result = safe_run_async(sign_in(phone, code), timeout=60)
    if result == "need_password":
        return render_template("verify_password.html", phone=phone)
        
    if is_session_string(result):
        groups = safe_run_async(get_groups(result), timeout=60)
        if not isinstance(groups, list): groups = []
        
        save_tg_account(phone=phone, owner_id=session['user_id'], session_string=result, cached_groups=groups)
        session.pop("pending_phone", None)
        flash("اکانت با موفقیت اضافه شد", "success")
        return redirect(url_for('dashboard'))
        
    flash(str(result), "danger")
    return redirect(url_for('add_account'))

@app.route("/verify_password", methods=["POST"])
@login_required
def verify_password():
    phone = session.get("pending_phone")
    password = request.form.get("password", "").strip()
    if not phone: return redirect(url_for('add_account'))
    
    result = safe_run_async(check_password(phone, password), timeout=60)
    if is_session_string(result):
        groups = safe_run_async(get_groups(result), timeout=60)
        if not isinstance(groups, list): groups = []
        
        save_tg_account(phone=phone, owner_id=session['user_id'], session_string=result, cached_groups=groups)
        session.pop("pending_phone", None)
        flash("اکانت با موفقیت اضافه شد", "success")
        return redirect(url_for('dashboard'))
        
    flash(str(result), "danger")
    return redirect(url_for('add_account'))

@app.route("/account/<phone>")
@login_required
def account_settings(phone):
    acc = get_tg_account(phone)
    if not acc or acc['owner_id'] != session['user_id']:
        flash("اکانت پیدا نشد", "danger")
        return redirect(url_for('dashboard'))
    return render_template("account_settings.html", acc=acc)

@app.route("/account/<phone>/save", methods=["POST"])
@login_required
def save_account_settings(phone):
    acc = get_tg_account(phone)
    if not acc or acc['owner_id'] != session['user_id']:
        return redirect(url_for('dashboard'))
        
    selected = request.form.getlist("groups")
    meow = request.form.get("meow_enabled") == "on"
    pishi = request.form.get("pishi_enabled") == "on"
    
    save_tg_account(
        phone=phone, owner_id=acc['owner_id'], session_string=acc['session_string'],
        selected_groups=selected, meow_enabled=meow, pishi_enabled=pishi,
        is_active=acc['is_active'], cached_groups=acc['cached_groups']
    )
    
    if acc['is_active']:
        workers.start_worker(phone, LOOP)
        
    flash("تنظیمات ذخیره شد", "success")
    return redirect(url_for('account_settings', phone=phone))

@app.route("/account/<phone>/toggle", methods=["POST"])
@login_required
def toggle_account(phone):
    acc = get_tg_account(phone)
    if not acc or acc['owner_id'] != session['user_id']:
        return redirect(url_for('dashboard'))
        
    new_state = not acc['is_active']
    save_tg_account(
        phone=phone, owner_id=acc['owner_id'], session_string=acc['session_string'],
        selected_groups=acc['selected_groups'], meow_enabled=acc['meow_enabled'],
        pishi_enabled=acc['pishi_enabled'], is_active=new_state, cached_groups=acc['cached_groups']
    )
    
    if new_state: workers.start_worker(phone, LOOP)
    else: workers.stop_worker(phone, LOOP)
        
    return redirect(url_for('dashboard'))

@app.route("/account/<phone>/delete", methods=["POST"])
@login_required
def delete_account(phone):
    acc = get_tg_account(phone)
    if acc and acc['owner_id'] == session['user_id']:
        workers.stop_worker(phone, LOOP)
        delete_tg_account(phone, session['user_id'])
        flash("اکانت حذف شد", "success")
    return redirect(url_for('dashboard'))

@app.route("/account/<phone>/refresh_groups", methods=["POST"])
@login_required
def refresh_groups(phone):
    acc = get_tg_account(phone)
    if not acc or acc['owner_id'] != session['user_id']:
        return redirect(url_for('dashboard'))
        
    groups = safe_run_async(get_groups(acc['session_string']), timeout=90)
    if isinstance(groups, list):
        save_tg_account(
            phone=phone, owner_id=acc['owner_id'], session_string=acc['session_string'],
            selected_groups=acc['selected_groups'], meow_enabled=acc['meow_enabled'],
            pishi_enabled=acc['pishi_enabled'], is_active=acc['is_active'], cached_groups=groups
        )
        flash(f"{len(groups)} گروه دریافت شد", "success")
    else:
        flash(f"خطا: {groups}", "danger")
    return redirect(url_for('account_settings', phone=phone))

@app.route("/admin/invites", methods=["GET", "POST"])
@admin_required
def admin_invites():
    if request.method == "POST":
        code = f"INV-{uuid.uuid4().hex[:8].upper()}"
        create_invite(code)
        flash(f"کد دعوت جدید ایجاد شد: {code}", "success")
        
    invites = get_all_invites()
    return render_template("admin_invites.html", invites=invites)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)