import os
import re
import uuid
import asyncio
import threading
import signal
import atexit

from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session
)

from config import SECRET_KEY

from database import (
    init_db,
    get_web_user,
    verify_web_user,
    create_web_user,
    get_valid_invite,
    use_invite,
    create_invite,
    get_all_invites,
    save_tg_account,
    get_tg_account,
    get_tg_accounts_for_user,
    delete_tg_account,
    get_setting,
    set_setting,
    get_all_settings,
    update_account_next_run
)

from clients import (
    send_code,
    sign_in,
    check_password
)

import workers
import session_manager


# ============================================================
# Background asyncio loop
# ============================================================

LOOP = asyncio.new_event_loop()


def _run_loop():
    asyncio.set_event_loop(LOOP)
    LOOP.run_forever()


threading.Thread(target=_run_loop, daemon=True).start()

try:
    workers._GLOBAL_LOOP = LOOP
except:
    pass

atexit.register(workers.shutdown_all_workers)

try:
    signal.signal(signal.SIGTERM, workers.shutdown_all_workers)
    signal.signal(signal.SIGINT, workers.shutdown_all_workers)
except:
    pass


def run_async(coro, timeout=None):
    future = asyncio.run_coroutine_threadsafe(coro, LOOP)
    return future.result(timeout=timeout)


def safe_run_async(coro, timeout=None):
    try:
        return run_async(coro, timeout=timeout)
    except Exception as e:
        return f"error: {str(e)}"


def is_error_result(result):
    return isinstance(result, str) and (
        result.startswith("error:")
        or result.startswith("خطا")
    )


def is_session_string(result):
    return (
        isinstance(result, str)
        and not is_error_result(result)
        and len(result) > 100
    )


# ============================================================
# Flask app
# ============================================================

app = Flask(__name__)
app.secret_key = SECRET_KEY

init_db()


# ============================================================
# Auth decorators
# ============================================================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))

        if not session.get("is_admin"):
            flash("دسترسی فقط برای ادمین", "danger")
            return redirect(url_for("dashboard"))

        return f(*args, **kwargs)
    return decorated


# ============================================================
# Helpers
# ============================================================

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def normalize_group_id(value: str):
    value = str(value or "").strip().translate(FA_DIGITS)
    value = re.sub(r"[^0-9-]", "", value)

    match = re.fullmatch(r"-?\d+", value)

    if match:
        return match.group(0)

    return None


def finalize_new_account(phone, session_string):
    """
    Saves the new account, waits a little, then fetches groups
    using the shared session manager.
    """
    save_tg_account(
        phone=phone,
        owner_id=session["user_id"],
        session_string=session_string,
        cached_groups=[]
    )

    session.pop("pending_phone", None)

    # Small pause after login before opening another client
    safe_run_async(asyncio.sleep(3), timeout=10)

    groups = safe_run_async(
        session_manager.get_groups_managed(phone),
        timeout=120
    )

    if isinstance(groups, list):
        account = get_tg_account(phone)

        if account:
            save_tg_account(
                phone=phone,
                owner_id=account["owner_id"],
                session_string=account["session_string"],
                selected_groups=account["selected_groups"],
                meow_enabled=account["meow_enabled"],
                pishi_enabled=account["pishi_enabled"],
                fishing_enabled=account["fishing_enabled"],
                is_active=account["is_active"],
                cached_groups=groups
            )

        return True, groups

    return False, groups


# ============================================================
# Basic routes
# ============================================================

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        user = verify_web_user(username, password)

        if user:
            session["user_id"] = user["id"]
            session["is_admin"] = bool(user["is_admin"])
            session["username"] = user["username"]

            return redirect(url_for("dashboard"))

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
            return redirect(url_for("register"))

        if get_web_user(username):
            flash("این نام کاربری قبلا ثبت شده است", "danger")
            return redirect(url_for("register"))

        if len(password) < 6:
            flash("رمز عبور باید حداقل ۶ کاراکتر باشد", "danger")
            return redirect(url_for("register"))

        create_web_user(username, password)

        new_user = get_web_user(username)
        use_invite(code, new_user["id"])

        session["user_id"] = new_user["id"]
        session["is_admin"] = bool(new_user["is_admin"])
        session["username"] = new_user["username"]

        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# Dashboard
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():
    accounts = get_tg_accounts_for_user(session["user_id"])
    return render_template("dashboard.html", accounts=accounts)


# ============================================================
# Add Telegram account
# ============================================================

@app.route("/add_account", methods=["GET", "POST"])
@login_required
def add_account():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()

        if not phone:
            flash("شماره را وارد کنید", "danger")
            return redirect(url_for("add_account"))

        existing = get_tg_account(phone)

        if existing and existing["owner_id"] != session["user_id"]:
            flash("این شماره قبلا ثبت شده است", "danger")
            return redirect(url_for("add_account"))

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

    if not phone:
        return redirect(url_for("add_account"))

    result = safe_run_async(sign_in(phone, code), timeout=60)

    if result == "need_password":
        return render_template("verify_password.html", phone=phone)

    if is_session_string(result):
        ok, groups = finalize_new_account(phone, result)

        if ok:
            flash("اکانت با موفقیت اضافه شد", "success")
        else:
            flash("اکانت اضافه شد، اما دریافت گروه‌ها ناموفق بود", "warning")

        return redirect(url_for("dashboard"))

    flash(str(result), "danger")
    return render_template("verify_code.html", phone=phone)


@app.route("/verify_password", methods=["POST"])
@login_required
def verify_password():
    phone = session.get("pending_phone")
    password = request.form.get("password", "").strip()

    if not phone:
        return redirect(url_for("add_account"))

    result = safe_run_async(check_password(phone, password), timeout=60)

    if is_session_string(result):
        ok, groups = finalize_new_account(phone, result)

        if ok:
            flash("اکانت با موفقیت اضافه شد", "success")
        else:
            flash("اکانت اضافه شد، اما دریافت گروه‌ها ناموفق بود", "warning")

        return redirect(url_for("dashboard"))

    flash(str(result), "danger")
    return render_template("verify_password.html", phone=phone)


# ============================================================
# Account settings
# ============================================================

@app.route("/account/<phone>/reset_timers", methods=["POST"])
@login_required
def reset_timers(phone):
    account = get_tg_account(phone)

    if not account or account["owner_id"] != session["user_id"]:
        flash("اکانت پیدا نشد", "danger")
        return redirect(url_for("dashboard"))

    update_account_next_run(
        phone,
        meow_next_run=0.0,
        pishi_next_run=0.0,
        fishing_next_run=0.0
    )

    flash("تایمرها ریست شدند", "success")

    return redirect(url_for("account_settings", phone=phone))

@app.route("/account/<phone>")
@login_required
def account_settings(phone):
    account = get_tg_account(phone)

    if not account or account["owner_id"] != session["user_id"]:
        flash("اکانت پیدا نشد", "danger")
        return redirect(url_for("dashboard"))

    return render_template("account_settings.html", acc=account)


@app.route("/account/<phone>/save", methods=["POST"])
@login_required
def save_account_settings(phone):
    account = get_tg_account(phone)

    if not account or account["owner_id"] != session["user_id"]:
        return redirect(url_for("dashboard"))

    selected_raw = request.form.getlist("groups")

    manual_id = request.form.get("manual_group_id", "").strip()
    if manual_id:
        selected_raw.append(manual_id)

    selected = []

    for group_id in selected_raw:
        normalized = normalize_group_id(group_id)

        if normalized and normalized not in selected:
            selected.append(normalized)

    meow = request.form.get("meow_enabled") == "on"
    pishi = request.form.get("pishi_enabled") == "on"
    fishing = request.form.get("fishing_enabled") == "on"

    save_tg_account(
        phone=phone,
        owner_id=account["owner_id"],
        session_string=account["session_string"],
        selected_groups=selected,
        meow_enabled=meow,
        pishi_enabled=pishi,
        fishing_enabled=fishing,
        is_active=account["is_active"],
        cached_groups=account["cached_groups"]
    )

    if account["is_active"]:
        workers.start_worker(phone, LOOP)

    flash("تنظیمات ذخیره شد", "success")
    return redirect(url_for("account_settings", phone=phone))


@app.route("/account/<phone>/toggle", methods=["POST"])
@login_required
def toggle_account(phone):
    account = get_tg_account(phone)

    if not account or account["owner_id"] != session["user_id"]:
        return redirect(url_for("dashboard"))

    new_state = not account["is_active"]

    save_tg_account(
        phone=phone,
        owner_id=account["owner_id"],
        session_string=account["session_string"],
        selected_groups=account["selected_groups"],
        meow_enabled=account["meow_enabled"],
        pishi_enabled=account["pishi_enabled"],
        fishing_enabled=account["fishing_enabled"],
        is_active=new_state,
        cached_groups=account["cached_groups"]
    )

    if new_state:
        workers.start_worker(phone, LOOP)
    else:
        workers.stop_worker(phone, LOOP)

    return redirect(url_for("dashboard"))


@app.route("/account/<phone>/delete", methods=["POST"])
@login_required
def delete_account(phone):
    account = get_tg_account(phone)

    if account and account["owner_id"] == session["user_id"]:
        workers.stop_worker(phone, LOOP)
        delete_tg_account(phone, session["user_id"])
        flash("اکانت حذف شد", "success")

    return redirect(url_for("dashboard"))


@app.route("/account/<phone>/refresh_groups", methods=["POST"])
@login_required
def refresh_groups(phone):
    account = get_tg_account(phone)

    if not account or account["owner_id"] != session["user_id"]:
        return redirect(url_for("dashboard"))

    groups = safe_run_async(
        session_manager.get_groups_managed(phone),
        timeout=120
    )

    if isinstance(groups, list):
        save_tg_account(
            phone=phone,
            owner_id=account["owner_id"],
            session_string=account["session_string"],
            selected_groups=account["selected_groups"],
            meow_enabled=account["meow_enabled"],
            pishi_enabled=account["pishi_enabled"],
            fishing_enabled=account["fishing_enabled"],
            is_active=account["is_active"],
            cached_groups=groups
        )

        flash(f"{len(groups)} گروه دریافت شد", "success")
    else:
        flash(f"خطا: {groups}", "danger")

    return redirect(url_for("account_settings", phone=phone))


# ============================================================
# Admin invites
# ============================================================

@app.route("/admin/invites", methods=["GET", "POST"])
@admin_required
def admin_invites():
    if request.method == "POST":
        code = f"INV-{uuid.uuid4().hex[:10].upper()}"
        create_invite(code)
        flash(f"کد دعوت جدید ایجاد شد: {code}", "success")

    invites = get_all_invites()

    return render_template("admin_invites.html", invites=invites)


# ============================================================
# Admin settings
# ============================================================

EDITABLE_SETTINGS = [
    {
        "key": "STARTUP_DELAY",
        "label": "Startup delay before starting workers (seconds)",
        "type": "number"
    },
    {
        "key": "ACCOUNT_START_INTERVAL",
        "label": "Delay between starting each account (seconds)",
        "type": "number"
    },
    {
        "key": "STOP_COOLDOWN_SECONDS",
        "label": "Cooldown after stopping a session (seconds)",
        "type": "number"
    },
    {
        "key": "PISHI_INTERVAL_SECONDS",
        "label": "Pishi interval (seconds) — 1800 = 30 minutes",
        "type": "number"
    },
    {
        "key": "DYNAMIC_WAIT_TIMEOUT_SECONDS",
        "label": "Dynamic parse timeout (seconds) — 0 = wait forever for parsed time",
        "type": "number"
    },
    {
        "key": "FISHING_CLICK_DELAY",
        "label": "Fishing click delay (seconds)",
        "type": "float"
    },
    {
        "key": "PISHI_CLICK_DELAY",
        "label": "Pishi click delay (seconds)",
        "type": "float"
    },
    {
        "key": "MEOW_CLICK_DELAY",
        "label": "Meow click delay (seconds)",
        "type": "float"
    },
    {
        "key": "TWO_PART_TIME_MODE",
        "label": "Two-part time mode (ms or hm)",
        "type": "text"
    },
]

@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        for item in EDITABLE_SETTINGS:
            key = item["key"]
            value = request.form.get(key, "").strip()

            if value == "":
                continue

            if item["type"] == "number":
                try:
                    int(value)
                except:
                    flash(f"{key} must be an integer", "danger")
                    continue

            elif item["type"] == "float":
                try:
                    float(value)
                except:
                    flash(f"{key} must be a number", "danger")
                    continue

            elif key == "TWO_PART_TIME_MODE":
                value = value.lower()

                if value not in ("ms", "hm"):
                    flash("TWO_PART_TIME_MODE must be ms or hm", "danger")
                    continue

            set_setting(key, value)

        flash("Settings saved", "success")
        return redirect(url_for("admin_settings"))

    settings = get_all_settings()

    return render_template(
        "admin_settings.html",
        settings=settings,
        editable_settings=EDITABLE_SETTINGS
    )


# ============================================================
# Startup
# ============================================================

try:
    workers.start_all_active(LOOP)
except Exception as e:
    print(f"Startup worker error: {e}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)