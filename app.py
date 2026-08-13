import os
import re
import uuid
import json
import time
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
    session,
    jsonify,
    Response
)

from config import SECRET_KEY

from database import (
    init_db,
    get_web_user,
    get_web_user_by_id,
    verify_web_user,
    create_web_user,
    get_valid_invite,
    use_invite,
    create_invite,
    get_all_invites,
    save_tg_account,
    get_tg_account,
    get_tg_account_by_uid,
    get_tg_accounts_for_user,
    delete_tg_account,
    get_setting,
    set_setting,
    get_all_settings,
    get_setting_int,
    update_account_next_run,
    reset_fishing_checks,
    get_account_logs,
    get_all_web_users_with_counts,
    set_user_disabled,
    set_backup_group_id,
    update_account_meta,
    mask_phone,
    create_job,
    get_active_jobs_for_user,
    get_heist_config,
    save_heist_config,
    get_heist_accounts,
    save_heist_accounts,
    get_heist_state,
    update_heist_state,
    get_all_heist_cooldowns,
    get_heist_logs,
)

from clients import (
    send_code,
    sign_in,
    check_password
)

import workers
import session_manager
import optimizations
import heist


# ============================================================
# Background asyncio loop
# ============================================================

LOOP = asyncio.new_event_loop()


def _run_loop():
    asyncio.set_event_loop(LOOP)
    optimizations.ensure_gc_task()
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
# Maintenance mode
# ============================================================

@app.before_request
def check_maintenance_mode():
    if request.path.startswith("/static"):
        return None

    try:
        maintenance = get_setting_int("MAINTENANCE_MODE", 0)
    except:
        maintenance = 0

    if maintenance != 1:
        return None

    if session.get("is_admin"):
        return None

    allowed_endpoints = ("login", "logout", "static")

    if request.endpoint in allowed_endpoints:
        return None

    return render_template("maintenance.html"), 503


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
    save_tg_account(
        phone=phone,
        owner_id=session["user_id"],
        session_string=session_string,
        cached_groups=[]
    )

    session.pop("pending_phone", None)

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
                rescue_enabled=account["rescue_enabled"],
                rescue_groups=account["rescue_groups"],
                is_active=account["is_active"],
                cached_groups=groups
            )

        try:
            name = safe_run_async(session_manager.get_me_name(phone), timeout=30)
            if isinstance(name, str) and not is_error_result(name) and name.strip():
                update_account_meta(phone, account_name=name.strip())
        except Exception:
            pass

        return True, groups

    return False, groups


def _save_account_active(account, new_state):
    save_tg_account(
        phone=account["phone"],
        owner_id=account["owner_id"],
        session_string=account["session_string"],
        selected_groups=account["selected_groups"],
        meow_enabled=account["meow_enabled"],
        pishi_enabled=account["pishi_enabled"],
        fishing_enabled=account["fishing_enabled"],
        rescue_enabled=account["rescue_enabled"],
        rescue_groups=account["rescue_groups"],
        is_active=new_state,
        cached_groups=account["cached_groups"]
    )


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

        if user and user.get("is_disabled"):
            flash("حساب شما غیرفعال شده است", "danger")
            return redirect(url_for("login"))

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

    user = get_web_user_by_id(session["user_id"])
    backup_group_id = (user or {}).get("backup_group_id") or ""

    total_balance = sum(acc.get("balance", 0) for acc in accounts)
    active_count = sum(1 for acc in accounts if acc.get("is_active"))
    error_count = sum(1 for acc in accounts if acc.get("session_status") == "error")

    active_jobs_list = get_active_jobs_for_user(session["user_id"])

    return render_template(
        "dashboard.html",
        accounts=accounts,
        backup_group_id=backup_group_id,
        total_balance=total_balance,
        active_count=active_count,
        error_count=error_count,
        active_jobs=active_jobs_list
    )


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
# Account settings (UID-based)
# ============================================================

@app.route("/account/<uid>/reset_timers", methods=["POST"])
@login_required
def reset_timers(uid):
    account = get_tg_account_by_uid(uid)

    if not account or account["owner_id"] != session["user_id"]:
        flash("اکانت پیدا نشد", "danger")
        return redirect(url_for("dashboard"))

    phone = account["phone"]

    update_account_next_run(phone, meow_next_run=0.0, pishi_next_run=0.0, fishing_next_run=0.0)
    reset_fishing_checks(phone)

    flash("تایمرها ریست شدند", "success")
    return redirect(url_for("account_settings", uid=uid))


@app.route("/account/<uid>")
@login_required
def account_settings(uid):
    account = get_tg_account_by_uid(uid)

    if not account or account["owner_id"] != session["user_id"]:
        flash("اکانت پیدا نشد", "danger")
        return redirect(url_for("dashboard"))

    return render_template("account_settings.html", acc=account)


@app.route("/account/<uid>/save", methods=["POST"])
@login_required
def save_account_settings(uid):
    account = get_tg_account_by_uid(uid)

    if not account or account["owner_id"] != session["user_id"]:
        return redirect(url_for("dashboard"))

    phone = account["phone"]

    selected_raw = request.form.getlist("groups")
    manual_id = request.form.get("manual_group_id", "").strip()
    if manual_id:
        selected_raw.append(manual_id)

    selected = []
    for group_id in selected_raw:
        normalized = normalize_group_id(group_id)
        if normalized and normalized not in selected:
            selected.append(normalized)

    rescue_raw = request.form.getlist("rescue_groups")
    manual_rescue_id = request.form.get("manual_rescue_group_id", "").strip()
    if manual_rescue_id:
        rescue_raw.append(manual_rescue_id)

    rescue_groups = []
    for group_id in rescue_raw:
        normalized = normalize_group_id(group_id)
        if normalized and normalized not in rescue_groups:
            rescue_groups.append(normalized)

    meow = request.form.get("meow_enabled") == "on"
    pishi = request.form.get("pishi_enabled") == "on"
    fishing = request.form.get("fishing_enabled") == "on"
    rescue = request.form.get("rescue_enabled") == "on"

    save_tg_account(
        phone=phone,
        owner_id=account["owner_id"],
        session_string=account["session_string"],
        selected_groups=selected,
        meow_enabled=meow,
        pishi_enabled=pishi,
        fishing_enabled=fishing,
        rescue_enabled=rescue,
        rescue_groups=rescue_groups,
        is_active=account["is_active"],
        cached_groups=account["cached_groups"]
    )

    if account["is_active"]:
        workers.start_worker(phone, LOOP)

    flash("تنظیمات ذخیره شد", "success")
    return redirect(url_for("account_settings", uid=uid))


@app.route("/account/<uid>/toggle", methods=["POST"])
@login_required
def toggle_account(uid):
    account = get_tg_account_by_uid(uid)

    if not account or account["owner_id"] != session["user_id"]:
        return redirect(url_for("dashboard"))

    phone = account["phone"]
    new_state = not account["is_active"]

    _save_account_active(account, new_state)

    if new_state:
        workers.start_worker(phone, LOOP)
    else:
        workers.stop_worker(phone, LOOP)

    return redirect(url_for("dashboard"))


@app.route("/account/<uid>/delete", methods=["POST"])
@login_required
def delete_account(uid):
    account = get_tg_account_by_uid(uid)

    if account and account["owner_id"] == session["user_id"]:
        phone = account["phone"]
        workers.stop_worker(phone, LOOP)
        delete_tg_account(phone, session["user_id"])
        flash("اکانت حذف شد", "success")

    return redirect(url_for("dashboard"))


@app.route("/account/<uid>/refresh_groups", methods=["POST"])
@login_required
def refresh_groups(uid):
    account = get_tg_account_by_uid(uid)

    if not account or account["owner_id"] != session["user_id"]:
        return redirect(url_for("dashboard"))

    phone = account["phone"]

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
            rescue_enabled=account["rescue_enabled"],
            rescue_groups=account["rescue_groups"],
            is_active=account["is_active"],
            cached_groups=groups
        )

        flash(f"{len(groups)} گروه دریافت شد", "success")
    else:
        flash(f"خطا: {groups}", "danger")

    return redirect(url_for("account_settings", uid=uid))


# ============================================================
# Account logs / phone reveal / feature toggle
# ============================================================

@app.route("/account/<uid>/logs")
@login_required
def account_logs(uid):
    account = get_tg_account_by_uid(uid)

    if not account or account["owner_id"] != session["user_id"]:
        flash("اکانت پیدا نشد", "danger")
        return redirect(url_for("dashboard"))

    logs = get_account_logs(uid, limit=200)

    return render_template("account_logs.html", acc=account, logs=logs)


@app.route("/account/<uid>/phone")
@login_required
def reveal_phone(uid):
    account = get_tg_account_by_uid(uid)

    if not account or account["owner_id"] != session["user_id"]:
        return jsonify({"phone": "***"}), 403

    return jsonify({"phone": account["phone"]})


@app.route("/account/<uid>/toggle_feature", methods=["POST"])
@login_required
def toggle_feature(uid):
    account = get_tg_account_by_uid(uid)

    if not account or account["owner_id"] != session["user_id"]:
        return jsonify({"error": "not found"}), 404

    data = request.get_json()
    feature = data.get("feature")
    enabled = bool(data.get("enabled", False))

    feature_map = {
        "meow": "meow_enabled",
        "pishi": "pishi_enabled",
        "fishing": "fishing_enabled",
        "rescue": "rescue_enabled",
    }

    if feature not in feature_map:
        return jsonify({"error": "invalid feature"}), 400

    phone = account["phone"]

    save_tg_account(
        phone=phone,
        owner_id=account["owner_id"],
        session_string=account["session_string"],
        selected_groups=account["selected_groups"],
        meow_enabled=enabled if feature == "meow" else account["meow_enabled"],
        pishi_enabled=enabled if feature == "pishi" else account["pishi_enabled"],
        fishing_enabled=enabled if feature == "fishing" else account["fishing_enabled"],
        rescue_enabled=enabled if feature == "rescue" else account["rescue_enabled"],
        rescue_groups=account["rescue_groups"],
        is_active=account["is_active"],
        cached_groups=account["cached_groups"]
    )

    return jsonify({"success": True})


# ============================================================
# Backup group / status / profile / transfer
# ============================================================

@app.route("/backup_group", methods=["POST"])
@login_required
def save_backup_group():
    value = request.form.get("backup_group_id", "").strip()
    normalized = normalize_group_id(value) if value else ""

    user = get_web_user_by_id(session["user_id"])
    old = (user or {}).get("backup_group_id") or ""

    set_backup_group_id(session["user_id"], normalized or "")

    try:
        workers.clear_backup_group_cache(session["user_id"])
    except Exception:
        pass

    if normalized and normalized != old:
        result = safe_run_async(
            workers.update_status_for_user(session["user_id"]),
            timeout=110
        )

        if is_error_result(result):
            flash("گروه بکاپ ذخیره شد؛ بررسی وضعیت در پس‌زمینه ادامه دارد", "warning")
        else:
            flash("گروه بکاپ ذخیره شد و وضعیت اکانت‌ها بررسی شد", "success")
    else:
        flash("گروه بکاپ ذخیره شد", "success")

    return redirect(url_for("dashboard"))


@app.route("/update_status", methods=["POST"])
@login_required
def update_status():
    accounts = get_tg_accounts_for_user(session["user_id"])

    if not accounts:
        flash("هیچ اکانتی برای بروزرسانی وجود ندارد", "warning")
        return redirect(url_for("dashboard"))

    job_id = create_job(session["user_id"], "update_status", len(accounts))

    result = safe_run_async(
        workers.update_status_for_user(session["user_id"], job_id=job_id),
        timeout=110
    )

    if is_error_result(result):
        flash("بروزرسانی در پس‌زمینه ادامه دارد", "warning")
    else:
        flash("نام و وضعیت گروه بکاپ اکانت‌ها بروزرسانی شد", "success")

    return redirect(url_for("dashboard"))


@app.route("/update_profiles", methods=["POST"])
@login_required
def update_profiles():
    user = get_web_user_by_id(session["user_id"])
    backup_group_id = (user or {}).get("backup_group_id") or ""

    if not backup_group_id:
        flash("ابتدا آیدی گروه بکاپ را تنظیم کنید", "danger")
        return redirect(url_for("dashboard"))

    accounts = get_tg_accounts_for_user(session["user_id"])

    if not accounts:
        flash("هیچ اکانتی برای بروزرسانی وجود ندارد", "warning")
        return redirect(url_for("dashboard"))

    job_id = create_job(session["user_id"], "update_profiles", len(accounts))

    result = safe_run_async(
        workers.update_profiles_for_user(session["user_id"], job_id=job_id),
        timeout=110
    )

    if is_error_result(result):
        flash("بروزرسانی میوهام در پس‌زمینه ادامه دارد", "warning")
    else:
        flash("اطلاعات اکانت‌ها (میوهام) بروزرسانی شد", "success")

    return redirect(url_for("dashboard"))


@app.route("/transfer", methods=["POST"])
@login_required
def transfer():
    user = get_web_user_by_id(session["user_id"])
    backup_group_id = (user or {}).get("backup_group_id") or ""

    if not backup_group_id:
        flash("ابتدا آیدی گروه بکاپ را تنظیم کنید", "danger")
        return redirect(url_for("dashboard"))

    target = str(request.form.get("target_user_id", "")).strip().translate(FA_DIGITS)

    if not re.fullmatch(r"\d+", target):
        flash("آیدی کاربر نامعتبر است", "danger")
        return redirect(url_for("dashboard"))

    accounts = get_tg_accounts_for_user(session["user_id"])

    if not accounts:
        flash("هیچ اکانتی برای انتقال وجود ندارد", "warning")
        return redirect(url_for("dashboard"))

    job_id = create_job(session["user_id"], "transfer", len(accounts))

    result = safe_run_async(
        workers.transfer_for_user(session["user_id"], target, job_id=job_id),
        timeout=110
    )

    if is_error_result(result):
        flash("انتقال در پس‌زمینه ادامه دارد", "warning")
    else:
        flash("دستور انتقال برای اکانت‌ها ارسال شد", "success")

    return redirect(url_for("dashboard"))


# ============================================================
# Bulk actions
# ============================================================

@app.route("/jobs/active")
@login_required
def active_jobs():
    jobs = get_active_jobs_for_user(session["user_id"])
    return jsonify(jobs)

@app.route("/bulk_start", methods=["POST"])
@login_required
def bulk_start():
    uids = request.form.getlist("uids[]")
    count = 0

    for uid in uids:
        account = get_tg_account_by_uid(uid)

        if not account or account["owner_id"] != session["user_id"]:
            continue

        phone = account["phone"]
        _save_account_active(account, True)
        workers.start_worker(phone, LOOP)
        count += 1

    flash(f"{count} اکانت فعال شد", "success")
    return redirect(url_for("dashboard"))


@app.route("/bulk_stop", methods=["POST"])
@login_required
def bulk_stop():
    uids = request.form.getlist("uids[]")
    count = 0

    for uid in uids:
        account = get_tg_account_by_uid(uid)

        if not account or account["owner_id"] != session["user_id"]:
            continue

        phone = account["phone"]
        _save_account_active(account, False)
        workers.stop_worker(phone, LOOP)
        count += 1

    flash(f"{count} اکانت غیرفعال شد", "success")
    return redirect(url_for("dashboard"))


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
    {"key": "STARTUP_DELAY", "label": "Startup delay before starting workers (seconds)", "type": "number"},
    {"key": "ACCOUNT_START_INTERVAL", "label": "Delay between starting each account (seconds)", "type": "number"},
    {"key": "STOP_COOLDOWN_SECONDS", "label": "Cooldown after stopping a session (seconds)", "type": "number"},
    {"key": "PISHI_INTERVAL_SECONDS", "label": "Pishi interval (seconds) — 1800 = 30 minutes", "type": "number"},
    {"key": "DYNAMIC_WAIT_TIMEOUT_SECONDS", "label": "Dynamic parse timeout for Meow (seconds) — 0 = wait forever", "type": "number"},
    {"key": "FISHING_STATUS_CHECK_DELAY", "label": "Fishing status check delay after successful button click (seconds)", "type": "number"},
    {"key": "FISHING_TIME_CHECK_INTERVAL", "label": "Fishing periodic time check interval (seconds)", "type": "number"},
    {"key": "BUTTON_CLICK_MAX_RETRIES", "label": "Button click max retries", "type": "number"},
    {"key": "BUTTON_CLICK_RETRY_DELAY", "label": "Button click retry delay (seconds)", "type": "float"},
    {"key": "FISHING_CLICK_DELAY", "label": "Fishing click delay (seconds)", "type": "float"},
    {"key": "PISHI_CLICK_DELAY", "label": "Pishi click delay (seconds)", "type": "float"},
    {"key": "MEOW_CLICK_DELAY", "label": "Meow click delay (seconds)", "type": "float"},
    {"key": "PROFILE_FETCH_TIMEOUT", "label": "میوهام reply timeout (seconds)", "type": "number"},
    {"key": "TWO_PART_TIME_MODE", "label": "Two-part time mode (ms or hm)", "type": "text"},
    {"key": "USER_JOB_CONCURRENCY", "label": "Concurrent account jobs (default 3)", "type": "number"},
    {"key": "STATUS_UPDATE_CONCURRENCY", "label": "Status update concurrency (0 or empty = global)", "type": "number"},
    {"key": "PROFILE_UPDATE_CONCURRENCY", "label": "Profile update concurrency (0 or empty = global)", "type": "number"},
    {"key": "TRANSFER_CONCURRENCY", "label": "Transfer concurrency (0 or empty = global)", "type": "number"},
    {"key": "TRANSFER_CONFIRM_TIMEOUT", "label": "Transfer confirmation message timeout (seconds)", "type": "number"},
    {"key": "TRANSFER_CONFIRM_EDIT_TIMEOUT", "label": "Transfer success edit timeout (seconds)", "type": "number"},
    {"key": "TRANSFER_CONFIRM_MAX_RETRIES", "label": "Transfer confirmation max retries", "type": "number"},
    {"key": "RESCUE_MAX_CLICKS", "label": "Rescue cat max clicks", "type": "number"},
    {"key": "RESCUE_FIRST_CLICK_DELAY", "label": "Rescue cat first click delay (seconds)", "type": "float"},
    {"key": "RESCUE_FAST_CLICK_MIN_DELAY", "label": "Rescue cat fast click min delay (seconds)", "type": "float"},
    {"key": "RESCUE_FAST_CLICK_MAX_DELAY", "label": "Rescue cat fast click max delay (seconds)", "type": "float"},
    {"key": "RESCUE_NORMAL_CLICK_DELAY", "label": "Rescue cat normal delay after first 4 clicks (seconds)", "type": "float"},
    {"key": "MEOW_COMMAND", "label": "Meow command", "type": "text"},
    {"key": "PISHI_COMMAND", "label": "Pishi command", "type": "text"},
    {"key": "FISHING_COMMAND", "label": "Fishing command", "type": "text"},
    {"key": "PROFILE_COMMAND", "label": "Profile command (میوهام)", "type": "text"},
    {"key": "TRANSFER_COMMAND_TEMPLATE", "label": "Transfer command template ({amount} {target})", "type": "text"},
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
# Admin automation
# ============================================================

GLOBAL_TOGGLE_ITEMS = [
    ("GLOBAL_AUTOMATION_ENABLED", "Master Automation"),
    ("GLOBAL_MEOW_ENABLED", "Meow"),
    ("GLOBAL_PISHI_ENABLED", "Pishi"),
    ("GLOBAL_FISHING_ENABLED", "Fishing"),
    ("GLOBAL_RESCUE_ENABLED", "Rescue"),
    ("GLOBAL_TRANSFER_ENABLED", "Transfer"),
    ("MAINTENANCE_MODE", "Maintenance Mode"),
]


@app.route("/admin/automation", methods=["GET", "POST"])
@admin_required
def admin_automation():
    if request.method == "POST":
        for key, label in GLOBAL_TOGGLE_ITEMS:
            enabled = request.form.get(key) == "on"
            set_setting(key, "1" if enabled else "0")

        flash("Automation toggles saved", "success")
        return redirect(url_for("admin_automation"))

    states = {}
    for key, label in GLOBAL_TOGGLE_ITEMS:
        states[key] = get_setting_int(key, 1 if key != "MAINTENANCE_MODE" else 0)

    return render_template(
        "admin_automation.html",
        items=GLOBAL_TOGGLE_ITEMS,
        states=states
    )


# ============================================================
# Admin users
# ============================================================

@app.route("/admin/users")
@admin_required
def admin_users():
    users = get_all_web_users_with_counts()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/toggle_disabled", methods=["POST"])
@admin_required
def toggle_user_disabled(user_id):
    if user_id == session.get("user_id"):
        flash("شما نمی‌توانید خودتان را غیرفعال کنید", "danger")
        return redirect(url_for("admin_users"))

    user = get_web_user_by_id(user_id)

    if not user:
        flash("کاربر پیدا نشد", "danger")
        return redirect(url_for("admin_users"))

    new_state = not user.get("is_disabled", False)
    set_user_disabled(user_id, new_state)

    if new_state:
        flash(f"کاربر {user['username']} غیرفعال شد", "success")
    else:
        flash(f"کاربر {user['username']} فعال شد", "success")

    return redirect(url_for("admin_users"))


# ============================================================
# Heist routes
# ============================================================

@app.route("/heist")
@login_required
def heist_panel():
    user_id = session["user_id"]

    config = get_heist_config(user_id) or {}
    accounts_assignments = get_heist_accounts(user_id)
    state = get_heist_state(user_id) or {"state": "idle"}
    cooldowns = get_all_heist_cooldowns(user_id)
    logs = get_heist_logs(user_id, limit=10)

    all_accounts = get_tg_accounts_for_user(user_id)

    # Build account details for assignments
    assignment_details = []
    for assignment in accounts_assignments:
        acc = get_tg_account(assignment["phone"])
        assignment_details.append({
            "phone": assignment["phone"],
            "position": assignment["position"],
            "uid": (acc or {}).get("uid", ""),
            "name": (acc or {}).get("account_name", ""),
            "masked_phone": mask_phone(assignment["phone"]),
            "is_active": (acc or {}).get("is_active", False),
            "jail_until": (acc or {}).get("jail_until", 0),
        })

    # Common groups
    common_groups = []
    if len(accounts_assignments) == 4:
        common_groups = heist.get_common_groups_sync(user_id)

    user = get_web_user_by_id(user_id)
    backup_group_id = (user or {}).get("backup_group_id") or ""

    now = int(time.time())

    return render_template(
        "heist.html",
        config=config,
        assignments=assignment_details,
        state=state,
        cooldowns=cooldowns,
        logs=logs,
        all_accounts=all_accounts,
        common_groups=common_groups,
        backup_group_id=backup_group_id,
        now=now,
    )


@app.route("/heist/config", methods=["POST"])
@login_required
def heist_save_config():
    user_id = session["user_id"]

    config = {
        "chat_id": int(request.form.get("chat_id", 0) or 0),
        "use_backup_group": request.form.get("use_backup_group") == "on",
        "selected_level": int(request.form.get("selected_level", 1) or 1),
        "auto_enabled": request.form.get("auto_enabled") == "on",
        "auto_level_mode": request.form.get("auto_level_mode", "best_available"),
        "steal_count": int(request.form.get("steal_count", 0) or 0),
        "move_count": int(request.form.get("move_count", 0) or 0),
        "listen_timeout": int(request.form.get("listen_timeout", 600) or 600),
        "heartbeat_enabled": request.form.get("heartbeat_enabled") == "on",
        "auto_delay_min": int(request.form.get("auto_delay_min", 5) or 5),
        "auto_delay_max": int(request.form.get("auto_delay_max", 15) or 15),
    }

    # If use_backup_group, set chat_id from user's backup group
    if config["use_backup_group"]:
        user = get_web_user_by_id(user_id)
        backup = (user or {}).get("backup_group_id") or ""
        try:
            config["chat_id"] = int(backup)
        except:
            config["chat_id"] = 0

    save_heist_config(user_id, config)

    # Manage auto heist loop
    if config["auto_enabled"]:
        workers.start_auto_heist(user_id, LOOP)
    else:
        workers.stop_auto_heist(user_id)

    flash("تنظیمات هیست ذخیره شد", "success")
    return redirect(url_for("heist_panel"))


@app.route("/heist/accounts", methods=["POST"])
@login_required
def heist_save_accounts():
    user_id = session["user_id"]

    phones = request.form.getlist("account_phones[]")

    if len(phones) != 4:
        flash("دقیقا ۴ اکانت انتخاب کنید", "danger")
        return redirect(url_for("heist_panel"))

    # Verify all phones belong to user
    user_accounts = get_tg_accounts_for_user(user_id)
    user_phones = {acc["phone"] for acc in user_accounts}

    for phone in phones:
        if phone not in user_phones:
            flash("یکی از اکانت‌ها متعلق به شما نیست", "danger")
            return redirect(url_for("heist_panel"))

    accounts = []
    for i, phone in enumerate(phones):
        accounts.append({"phone": phone, "position": i + 1})

    save_heist_accounts(user_id, accounts)

    flash("اکانت‌های هیست ذخیره شدند", "success")
    return redirect(url_for("heist_panel"))


@app.route("/heist/start", methods=["POST"])
@login_required
def heist_start():
    user_id = session["user_id"]

    # Check for custom override
    use_custom = request.form.get("use_custom") == "on"

    config_override = None

    if use_custom:
        base_config = get_heist_config(user_id) or {}
        config_override = dict(base_config)

        custom_level = request.form.get("custom_level")
        if custom_level:
            config_override["selected_level"] = int(custom_level)

        custom_steal = request.form.get("custom_steal_count")
        if custom_steal is not None and custom_steal != "":
            config_override["steal_count"] = int(custom_steal)

        custom_move = request.form.get("custom_move_count")
        if custom_move is not None and custom_move != "":
            config_override["move_count"] = int(custom_move)

    # Run heist in background
    async def _run():
        result = await heist.run_heist(user_id, config_override=config_override)

        if result.get("error"):
            print(f"❌ Heist error for user {user_id}: {result['error']}")
        else:
            print(f"✅ Heist completed for user {user_id}: {result.get('result')}")

    try:
        asyncio.run_coroutine_threadsafe(_run(), LOOP)
        flash("هیست شروع شد", "success")
    except Exception as e:
        flash(f"خطا در شروع هیست: {e}", "danger")

    return redirect(url_for("heist_panel"))


@app.route("/heist/abort", methods=["POST"])
@login_required
def heist_abort():
    user_id = session["user_id"]
    heist.request_abort(user_id)
    flash("درخواست لغو هیست ارسال شد", "warning")
    return redirect(url_for("heist_panel"))


@app.route("/heist/state")
@login_required
def heist_state():
    user_id = session["user_id"]

    state = get_heist_state(user_id) or {"state": "idle"}
    config = get_heist_config(user_id) or {}
    cooldowns = get_all_heist_cooldowns(user_id)

    # Get account UIDs instead of phones
    assignments = get_heist_accounts(user_id)
    account_uids = []
    for a in assignments:
        acc = get_tg_account(a["phone"])
        account_uids.append((acc or {}).get("uid", a["phone"]))

    now = int(time.time())

    # Check jail status for all accounts
    jail_info = {}
    for a in assignments:
        acc = get_tg_account(a["phone"])
        if acc:
            jail_info[(acc or {}).get("uid", a["phone"])] = {
                "jail_until": acc.get("jail_until", 0),
                "is_jailed": acc.get("jail_until", 0) > now,
            }

    payload = {
        "state": state.get("state", "idle"),
        "level": state.get("level", 0),
        "steal_clicks_done": state.get("steal_clicks_done", 0),
        "steal_clicks_target": config.get("steal_count", 0),
        "move_clicks_done": state.get("move_clicks_done", 0),
        "move_clicks_target": config.get("move_count", 0),
        "accounts": account_uids,
        "started_at": state.get("started_at", 0),
        "updated_at": state.get("updated_at", 0),
        "error": state.get("error_message", ""),
        "cooldowns": cooldowns,
        "jail": jail_info,
        "now": now,
    }

    return jsonify(payload)


@app.route("/heist/check_cooldowns", methods=["POST"])
@login_required
def heist_check_cooldowns():
    user_id = session["user_id"]

    result = safe_run_async(
        heist.check_heist_cooldowns(user_id),
        timeout=60
    )

    if isinstance(result, dict) and result.get("error"):
        flash(f"خطا در بررسی کولداون: {result['error']}", "danger")
    elif isinstance(result, dict) and result.get("levels"):
        flash("کولداون‌ها بروزرسانی شدند", "success")
    else:
        flash("بررسی کولداون انجام شد", "info")

    return redirect(url_for("heist_panel"))


@app.route("/heist/check_jail", methods=["POST"])
@login_required
def heist_check_jail():
    user_id = session["user_id"]
    phone = request.form.get("phone", "").strip()

    if not phone:
        flash("اکانت مشخص نشده است", "danger")
        return redirect(url_for("heist_panel"))

    # Verify ownership
    account = get_tg_account(phone)
    if not account or account["owner_id"] != user_id:
        flash("اکانت پیدا نشد", "danger")
        return redirect(url_for("heist_panel"))

    result = safe_run_async(
        heist.check_jail_status(user_id, phone),
        timeout=30
    )

    if isinstance(result, dict):
        if result.get("in_jail"):
            flash(f"اکانت در زندان است ({result.get('seconds', 0)} ثانیه باقیمانده)", "warning")
        else:
            flash("اکانت در زندان نیست", "success")
    else:
        flash("بررسی زندان انجام شد", "info")

    return redirect(url_for("heist_panel"))


@app.route("/heist/common_groups")
@login_required
def heist_common_groups():
    user_id = session["user_id"]
    groups = heist.get_common_groups_sync(user_id)
    return jsonify(groups)


@app.route("/heist/logs")
@login_required
def heist_logs():
    user_id = session["user_id"]
    logs = get_heist_logs(user_id, limit=20)
    return jsonify(logs)


# ============================================================
# Startup
# ============================================================

try:
    workers.start_all_active(LOOP)
except Exception as e:
    print(f"Startup worker error: {e}")

# Start auto heist loops for users who have it enabled
try:
    from database import get_heist_config as _ghc
    # We can't easily iterate all users here without a query,
    # so auto heist loops are started when config is saved
    # or when the heist panel is visited
except Exception:
    pass


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)