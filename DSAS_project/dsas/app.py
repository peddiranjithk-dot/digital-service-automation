"""
Digital Service Automation System (DSAS)
--------------------------------------------------------
A Flask application that AUTOMATES digital service-request handling
using a configurable rules engine + a background scheduler, instead
of relying on manual admin triage.

Core idea
---------
Admins define AutomationRules:  IF <condition> THEN <action>
Rules fire on three kinds of triggers:
  - on_create           : evaluated the instant a request is submitted
  - on_status_change     : evaluated whenever a request's status changes
  - scheduled             : evaluated periodically by a background job
                            (e.g. SLA breach checks / auto-escalation /
                            auto-close of stale resolved tickets)

Every action a rule performs is written to an AutomationLog so the
automation is fully auditable, and affected users get a Notification.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000

Default admin login (auto-seeded):
    email:    admin@dsas.com
    password: admin123
"""

import os
from datetime import datetime

from flask import (Flask, render_template, redirect, url_for, flash,
                    request, jsonify, abort)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                          login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# --------------------------------------------------------------------------
# App configuration
# --------------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-secret-key-in-production"
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'dsas.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "warning"

CATEGORIES = ["Network", "Hardware", "Software", "Account & Access",
              "Billing", "General"]
PRIORITIES = ["Low", "Medium", "High", "Critical"]
STATUSES = ["Open", "In Progress", "Resolved", "Closed"]

TRIGGER_TYPES = ["on_create", "on_status_change", "scheduled"]
CONDITION_FIELDS = ["category", "priority", "status", "age_hours",
                     "hours_since_resolved"]
CONDITION_OPERATORS = ["equals", "not_equals", "greater_than", "less_than"]
ACTION_TYPES = ["set_priority", "set_status", "escalate_priority",
                "assign_user", "notify"]


# --------------------------------------------------------------------------
# Database Models
# --------------------------------------------------------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user")  # user | admin
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"


class ServiceRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), nullable=False, default="General")
    priority = db.Column(db.String(20), nullable=False, default="Medium")
    status = db.Column(db.String(20), nullable=False, default="Open")

    requester_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)

    requester = db.relationship("User", foreign_keys=[requester_id])
    assignee = db.relationship("User", foreign_keys=[assigned_to_id])

    def age_hours(self):
        return round((datetime.utcnow() - self.created_at).total_seconds() / 3600, 2)

    def hours_since_resolved(self):
        if self.resolved_at:
            return round((datetime.utcnow() - self.resolved_at).total_seconds() / 3600, 2)
        return None


class AutomationRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    trigger_type = db.Column(db.String(30), nullable=False)  # on_create/on_status_change/scheduled

    condition_field = db.Column(db.String(30), nullable=False)
    condition_operator = db.Column(db.String(20), nullable=False)
    condition_value = db.Column(db.String(100), nullable=False)

    action_type = db.Column(db.String(30), nullable=False)
    action_value = db.Column(db.String(200), nullable=True)

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    run_count = db.Column(db.Integer, default=0)


class AutomationLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("service_request.id"), nullable=False)
    rule_id = db.Column(db.Integer, db.ForeignKey("automation_rule.id"), nullable=True)
    action_taken = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    request = db.relationship("ServiceRequest")
    rule = db.relationship("AutomationRule")


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    request_id = db.Column(db.Integer, db.ForeignKey("service_request.id"), nullable=True)
    message = db.Column(db.String(300), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# --------------------------------------------------------------------------
# Automation Rules Engine
# --------------------------------------------------------------------------
PRIORITY_ORDER = ["Low", "Medium", "High", "Critical"]


def _actual_value(req: ServiceRequest, field: str):
    if field == "category":
        return req.category
    if field == "priority":
        return req.priority
    if field == "status":
        return req.status
    if field == "age_hours":
        return req.age_hours()
    if field == "hours_since_resolved":
        return req.hours_since_resolved()
    return None


def evaluate_condition(rule: AutomationRule, req: ServiceRequest) -> bool:
    actual = _actual_value(req, rule.condition_field)
    if actual is None:
        return False  # e.g. hours_since_resolved requested but ticket never resolved

    if rule.condition_field in ("age_hours", "hours_since_resolved"):
        try:
            actual_num = float(actual)
            target_num = float(rule.condition_value)
        except (TypeError, ValueError):
            return False
        if rule.condition_operator == "greater_than":
            return actual_num > target_num
        if rule.condition_operator == "less_than":
            return actual_num < target_num
        if rule.condition_operator == "equals":
            return actual_num == target_num
        return False

    actual_str = str(actual).strip().lower()
    target_str = str(rule.condition_value).strip().lower()
    if rule.condition_operator == "equals":
        return actual_str == target_str
    if rule.condition_operator == "not_equals":
        return actual_str != target_str
    return False


def execute_action(rule: AutomationRule, req: ServiceRequest) -> str:
    """Performs the rule's action on the request and returns a
    human-readable description of what happened (for the audit log)."""
    action, value = rule.action_type, (rule.action_value or "").strip()
    note = ""

    if action == "set_priority" and value in PRIORITIES:
        req.priority = value
        note = f"Priority automatically set to '{value}'"

    elif action == "set_status" and value in STATUSES:
        req.status = value
        if value in ("Resolved", "Closed") and not req.resolved_at:
            req.resolved_at = datetime.utcnow()
        note = f"Status automatically set to '{value}'"

    elif action == "escalate_priority":
        idx = PRIORITY_ORDER.index(req.priority) if req.priority in PRIORITY_ORDER else 0
        if idx < len(PRIORITY_ORDER) - 1:
            req.priority = PRIORITY_ORDER[idx + 1]
            note = f"Priority auto-escalated to '{req.priority}' (SLA rule)"
        else:
            note = "Priority already at maximum (Critical) — escalation skipped"

    elif action == "assign_user":
        user = User.query.filter_by(email=value).first()
        if user:
            req.assigned_to_id = user.id
            note = f"Auto-assigned to {user.name}"
        else:
            note = f"Auto-assign failed — no user with email '{value}'"

    elif action == "notify":
        target_id = req.assigned_to_id or req.requester_id
        msg = value or f"Automated update on request #{req.id}: {rule.name}"
        db.session.add(Notification(user_id=target_id, request_id=req.id, message=msg))
        note = f"Notification sent: \"{msg}\""

    else:
        note = f"Unrecognized action '{action}'"

    rule.run_count = (rule.run_count or 0) + 1
    db.session.add(AutomationLog(request_id=req.id, rule_id=rule.id, action_taken=note))
    return note


def run_automation(trigger_type: str, req: ServiceRequest = None):
    """Evaluate all active rules of `trigger_type`.
    - For on_create / on_status_change: only against the given `req`.
    - For scheduled: against every non-closed request in the system.
    Returns a list of (request_id, rule_name, note) tuples actually fired.
    """
    rules = AutomationRule.query.filter_by(trigger_type=trigger_type, is_active=True).all()
    if not rules:
        return []

    if req is not None:
        targets = [req]
    else:
        targets = ServiceRequest.query.filter(ServiceRequest.status != "Closed").all()

    fired = []
    for target in targets:
        for rule in rules:
            if evaluate_condition(rule, target):
                note = execute_action(rule, target)
                fired.append((target.id, rule.name, note))
    db.session.commit()
    return fired


def scheduled_automation_job():
    """Runs inside the background scheduler thread — needs its own app context."""
    with app.app_context():
        fired = run_automation("scheduled")
        if fired:
            print(f"[scheduler] {len(fired)} automated action(s) applied "
                  f"at {datetime.utcnow().isoformat()}")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def admin_required(func):
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return func(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_unread_count():
    if current_user.is_authenticated:
        count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
        return {"unread_count": count}
    return {"unread_count": 0}


# --------------------------------------------------------------------------
# Routes: Auth
# --------------------------------------------------------------------------
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("admin_dashboard" if current_user.is_admin
                                 else "user_dashboard"))
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("All fields are required.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "danger")
        else:
            user = User(name=name, email=email, role="user")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            flash(f"Welcome back, {user.name}!", "success")
            return redirect(url_for("index"))
        flash("Invalid email or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Routes: User side
# --------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def user_dashboard():
    if current_user.is_admin:
        return redirect(url_for("admin_dashboard"))
    reqs = (ServiceRequest.query.filter_by(requester_id=current_user.id)
            .order_by(ServiceRequest.created_at.desc()).all())
    return render_template("dashboard_user.html", requests=reqs)


@app.route("/requests/new", methods=["GET", "POST"])
@login_required
def new_request():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "General")
        priority = request.form.get("priority", "Medium")

        if not title or not description:
            flash("Title and description are required.", "danger")
            return render_template("new_request.html", categories=CATEGORIES,
                                    priorities=PRIORITIES)

        req = ServiceRequest(title=title, description=description,
                              category=category, priority=priority,
                              status="Open", requester_id=current_user.id)
        db.session.add(req)
        db.session.commit()

        fired = run_automation("on_create", req)
        if fired:
            flash(f"Request #{req.id} submitted — {len(fired)} automation "
                  f"rule(s) applied instantly.", "success")
        else:
            flash(f"Request #{req.id} submitted.", "success")
        return redirect(url_for("request_detail", request_id=req.id))

    return render_template("new_request.html", categories=CATEGORIES,
                            priorities=PRIORITIES)


@app.route("/requests/<int:request_id>")
@login_required
def request_detail(request_id):
    req = ServiceRequest.query.get_or_404(request_id)
    if not current_user.is_admin and req.requester_id != current_user.id:
        abort(403)
    logs = (AutomationLog.query.filter_by(request_id=req.id)
            .order_by(AutomationLog.created_at.desc()).all())
    return render_template("request_detail.html", req=req, logs=logs,
                            statuses=STATUSES, priorities=PRIORITIES)


@app.route("/notifications")
@login_required
def notifications():
    notes = (Notification.query.filter_by(user_id=current_user.id)
             .order_by(Notification.created_at.desc()).all())
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    return render_template("notifications.html", notes=notes)


# --------------------------------------------------------------------------
# Routes: Admin side
# --------------------------------------------------------------------------
@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    reqs = ServiceRequest.query.order_by(ServiceRequest.created_at.desc()).all()
    stats = {
        "total": ServiceRequest.query.count(),
        "open": ServiceRequest.query.filter_by(status="Open").count(),
        "in_progress": ServiceRequest.query.filter_by(status="In Progress").count(),
        "resolved": ServiceRequest.query.filter_by(status="Resolved").count(),
        "closed": ServiceRequest.query.filter_by(status="Closed").count(),
        "active_rules": AutomationRule.query.filter_by(is_active=True).count(),
        "automated_actions": AutomationLog.query.count(),
    }
    return render_template("dashboard_admin.html", requests=reqs, stats=stats)


@app.route("/admin/requests/<int:request_id>/update", methods=["POST"])
@login_required
@admin_required
def update_request(request_id):
    req = ServiceRequest.query.get_or_404(request_id)
    old_status = req.status

    new_status = request.form.get("status")
    priority = request.form.get("priority")
    if priority in PRIORITIES:
        req.priority = priority
    if new_status in STATUSES:
        req.status = new_status
        if new_status in ("Resolved", "Closed") and not req.resolved_at:
            req.resolved_at = datetime.utcnow()
        if new_status not in ("Resolved", "Closed"):
            req.resolved_at = None
    db.session.commit()

    fired = []
    if new_status and new_status != old_status:
        fired = run_automation("on_status_change", req)

    msg = f"Request #{req.id} updated."
    if fired:
        msg += f" {len(fired)} automation rule(s) triggered by the status change."
    flash(msg, "success")
    return redirect(url_for("request_detail", request_id=req.id))


@app.route("/admin/automation/run", methods=["POST"])
@login_required
@admin_required
def run_scheduled_now():
    fired = run_automation("scheduled")
    if fired:
        flash(f"Automation run complete — {len(fired)} action(s) applied "
              f"across {len(set(f[0] for f in fired))} request(s).", "success")
    else:
        flash("Automation run complete — no rules matched any active request.", "info")
    return redirect(url_for("admin_dashboard"))


# --- Rule management -------------------------------------------------------
@app.route("/admin/rules")
@login_required
@admin_required
def rules_list():
    rules = AutomationRule.query.order_by(AutomationRule.created_at.desc()).all()
    return render_template("rules_list.html", rules=rules)


@app.route("/admin/rules/new", methods=["GET", "POST"])
@login_required
@admin_required
def new_rule():
    if request.method == "POST":
        rule = AutomationRule(
            name=request.form.get("name", "").strip(),
            trigger_type=request.form.get("trigger_type"),
            condition_field=request.form.get("condition_field"),
            condition_operator=request.form.get("condition_operator"),
            condition_value=request.form.get("condition_value", "").strip(),
            action_type=request.form.get("action_type"),
            action_value=request.form.get("action_value", "").strip(),
            is_active=True,
        )
        if not rule.name or not rule.condition_value:
            flash("Rule name and condition value are required.", "danger")
        else:
            db.session.add(rule)
            db.session.commit()
            flash(f"Automation rule '{rule.name}' created.", "success")
            return redirect(url_for("rules_list"))

    return render_template("new_rule.html", trigger_types=TRIGGER_TYPES,
                            condition_fields=CONDITION_FIELDS,
                            condition_operators=CONDITION_OPERATORS,
                            action_types=ACTION_TYPES, statuses=STATUSES,
                            priorities=PRIORITIES, categories=CATEGORIES)


@app.route("/admin/rules/<int:rule_id>/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_rule(rule_id):
    rule = AutomationRule.query.get_or_404(rule_id)
    rule.is_active = not rule.is_active
    db.session.commit()
    flash(f"Rule '{rule.name}' {'activated' if rule.is_active else 'deactivated'}.", "info")
    return redirect(url_for("rules_list"))


@app.route("/admin/rules/<int:rule_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_rule(rule_id):
    rule = AutomationRule.query.get_or_404(rule_id)
    db.session.delete(rule)
    db.session.commit()
    flash("Rule deleted.", "info")
    return redirect(url_for("rules_list"))


@app.route("/admin/logs")
@login_required
@admin_required
def automation_logs():
    logs = (AutomationLog.query.order_by(AutomationLog.created_at.desc())
            .limit(200).all())
    return render_template("automation_logs.html", logs=logs)


@app.route("/api/automation_stats")
@login_required
@admin_required
def api_automation_stats():
    rules = AutomationRule.query.all()
    by_status = {s: ServiceRequest.query.filter_by(status=s).count() for s in STATUSES}
    return jsonify({
        "total_requests": ServiceRequest.query.count(),
        "total_actions": AutomationLog.query.count(),
        "active_rules": sum(1 for r in rules if r.is_active),
        "by_status": by_status,
        "rule_usage": {r.name: r.run_count for r in rules},
    })


# --------------------------------------------------------------------------
# Error handlers
# --------------------------------------------------------------------------
@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403,
                            message="You don't have permission to view this page."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page not found."), 404


# --------------------------------------------------------------------------
# DB bootstrap + default automation rules
# --------------------------------------------------------------------------
def seed_data():
    db.create_all()

    if not User.query.filter_by(email="admin@dsas.com").first():
        admin = User(name="System Admin", email="admin@dsas.com", role="admin")
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()
        print("Default admin created -> admin@dsas.com / admin123")

    if AutomationRule.query.count() == 0:
        default_rules = [
            AutomationRule(
                name="Auto-prioritize Network Issues",
                trigger_type="on_create", condition_field="category",
                condition_operator="equals", condition_value="Network",
                action_type="set_priority", action_value="High"),
            AutomationRule(
                name="Auto-assign Account & Access requests to Admin",
                trigger_type="on_create", condition_field="category",
                condition_operator="equals", condition_value="Account & Access",
                action_type="assign_user", action_value="admin@dsas.com"),
            AutomationRule(
                name="Notify on Critical request creation",
                trigger_type="on_create", condition_field="priority",
                condition_operator="equals", condition_value="Critical",
                action_type="notify",
                action_value="A new CRITICAL request was just created and needs attention."),
            AutomationRule(
                name="SLA Escalation — Open over 24h",
                trigger_type="scheduled", condition_field="age_hours",
                condition_operator="greater_than", condition_value="24",
                action_type="escalate_priority", action_value=""),
            AutomationRule(
                name="Auto-close Resolved requests after 48h",
                trigger_type="scheduled", condition_field="hours_since_resolved",
                condition_operator="greater_than", condition_value="48",
                action_type="set_status", action_value="Closed"),
        ]
        db.session.add_all(default_rules)
        db.session.commit()
        print(f"Seeded {len(default_rules)} default automation rules")


# --------------------------------------------------------------------------
# Background scheduler (runs the "scheduled" automation rules periodically)
# --------------------------------------------------------------------------
scheduler = BackgroundScheduler(daemon=True)


def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(scheduled_automation_job, "interval", seconds=60,
                           id="scheduled_automation", replace_existing=True)
        scheduler.start()
        print("Background automation scheduler started (runs every 60s).")


if __name__ == "__main__":
    with app.app_context():
        seed_data()

    # Avoid starting the scheduler twice under the Flask debug reloader
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        start_scheduler()

    app.run(debug=True)
