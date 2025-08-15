#!/usr/bin/env python3
"""
Quick start (macOS/Linux/Windows)
---------------------------------
1) Save this file as `app.py`.
2) Create a virtual env & install deps:
   python -m venv .venv && source .venv/bin/activate
   pip install flask flask_sqlalchemy flask_login python-dotenv
3) Run:
   flask --app times.py --debug run
4) Open http://127.0.0.1:5000

Logins (change in .env or later in DB):
   admin / admin
   tim   / tim
   zach  / zach

"""

from __future__ import annotations
import csv
import io
import os
from datetime import datetime, timedelta, date
from typing import Optional

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    current_user,
    logout_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

# ----------------------------------------------------------------------------
# App & DB setup
# ----------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///timesheet.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    prefix = db.Column(db.Integer, nullable=False, unique=True, index=True)  # 4-digit series
    division = db.Column(db.String(50), nullable=False, default="Melbourne Power")  # "Melbourne Power" | "Liquid Pack"
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    def as_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "prefix": self.prefix,
            "division": self.division,
            "is_active": self.is_active,
        }


class TimeEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    notes = db.Column(db.String(500), nullable=True)
    # Travel flags
    travel_morning = db.Column(db.Boolean, nullable=False, default=False)
    travel_afternoon = db.Column(db.Boolean, nullable=False, default=False)

    project = db.relationship("Project")
    user = db.relationship("User")

    def duration_hours(self) -> float:
        return (self.end_time - self.start_time).total_seconds() / 3600.0

    def as_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "creator_username": self.user.username if self.user else None,
            "project_id": self.project_id,
            "project_title": self.project.title if self.project else None,
            "project_prefix": self.project.prefix if self.project else None,
            "project_division": self.project.division if self.project else None,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "notes": self.notes or "",
            "duration_hours": round(self.duration_hours(), 3),
            "travel_morning": bool(self.travel_morning),
            "travel_afternoon": bool(self.travel_afternoon),
        }


# ----------------------------------------------------------------------------
# Auth & bootstrap
# ----------------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    return User.query.get(int(user_id))


def ensure_schema():
    """Tiny auto-migration for added columns if you ran an older version."""
    db.create_all()

    # Ensure "division" on project
    cols_proj = [row[1] for row in db.session.execute(text("PRAGMA table_info(project)"))]
    if "division" not in cols_proj:
        db.session.execute(
            text("ALTER TABLE project ADD COLUMN division VARCHAR(50) NOT NULL DEFAULT 'Melbourne Power'")
        )
        db.session.commit()

    # Ensure travel columns on time_entry
    cols_te = [row[1] for row in db.session.execute(text("PRAGMA table_info(time_entry)"))]
    changed = False
    if "travel_morning" not in cols_te:
        db.session.execute(text("ALTER TABLE time_entry ADD COLUMN travel_morning BOOLEAN NOT NULL DEFAULT 0"))
        changed = True
    if "travel_afternoon" not in cols_te:
        db.session.execute(text("ALTER TABLE time_entry ADD COLUMN travel_afternoon BOOLEAN NOT NULL DEFAULT 0"))
        changed = True
    if changed:
        db.session.commit()


def create_user_if_missing(username: str, password: str):
    u = User.query.filter_by(username=username).first()
    if not u:
        u = User(username=username)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()


def bootstrap_admin():
    ensure_schema()
    create_user_if_missing(os.getenv("ADMIN_USERNAME", "admin"), os.getenv("ADMIN_PASSWORD", "admin"))
    # Additional requested users
    create_user_if_missing("tim", "tim")
    create_user_if_missing("zach", "zach")


with app.app_context():
    bootstrap_admin()

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
VALID_DIVISIONS = ("Melbourne Power", "Liquid Pack")


def next_project_prefix(division: str) -> int:
    """Return the next 4-digit prefix for a division.
    Melbourne Power starts at 1000; Liquid Pack starts at 2000.

    Machine-ID projects may use custom prefixes outside the 4‑digit ranges.
    This helper ignores any custom prefixes when determining the next auto-prefix
    by only considering prefixes within the 4‑digit range for each division.
    """
    base = 1000 if division == "Melbourne Power" else 2000
    upper = base + 1000  # 2000 for MP and 3000 for LP
    # Only consider prefixes in the auto-range [base, upper)
    last = (
        db.session.query(db.func.max(Project.prefix))
        .filter(Project.division == division, Project.prefix >= base, Project.prefix < upper)
        .scalar()
    )
    if last is None or last < base:
        return base
    return last + 1


def parse_dt(s: str) -> datetime:
    """Accept 'YYYY-MM-DDTHH:MM' or full ISO8601."""
    try:
        if "T" in s and len(s) == 16:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M")
        return datetime.fromisoformat(s)
    except Exception:
        raise ValueError("Invalid datetime format. Use ISO 'YYYY-MM-DDTHH:MM'.")


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------
@app.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("app_page"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.form or request.json or {}
        username = data.get("username", "").strip()
        password = data.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("app_page"))
        return render_template_string(LOGIN_HTML, error="Invalid credentials")
    return render_template_string(LOGIN_HTML)


@app.route("/logout", methods=["POST"])  # POST to avoid CSRF-ish GET logout
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/app")
@login_required
def app_page():
    return render_template_string(APP_HTML)


@app.route("/projects")
@login_required
def projects_page():
    return render_template_string(PROJECTS_HTML)


@app.route("/review")
@login_required
def review_page():
    return render_template_string(REVIEW_HTML)


@app.route("/admin")
@login_required
def admin_page():
    return render_template_string(ADMIN_HTML)

# ----------------------------------------------------------------------------
# API — Users (for Review filter)
# ----------------------------------------------------------------------------
@app.route("/api/users", methods=["GET"])
@login_required
def api_users():
    users = User.query.order_by(User.username.asc()).all()
    return jsonify([{"id": u.id, "username": u.username} for u in users])

# ----------------------------------------------------------------------------
# API — Projects
# ----------------------------------------------------------------------------
@app.route("/api/projects", methods=["GET", "POST"])
@login_required
def api_projects():
    if request.method == "GET":
        projects = Project.query.order_by(Project.division.asc(), Project.prefix.asc()).all()
        return jsonify([p.as_dict() for p in projects])

    data = request.get_json() or request.form
    title = (data.get("title") or "").strip()
    division = (data.get("division") or "").strip() or "Melbourne Power"
    # Validate division
    if division not in VALID_DIVISIONS:
        return jsonify({"error": f"division must be one of {VALID_DIVISIONS}"}), 400
    # Title is required
    if not title:
        return jsonify({"error": "Title is required"}), 400

    # Check for optional custom prefix (for Machine ID projects)
    prefix_field = data.get("prefix")
    prefix: Optional[int] = None
    if prefix_field is not None and prefix_field != "":
        # Only allow custom prefix for Liquid Pack machine ID projects
        if division != "Liquid Pack":
            return jsonify({"error": "Custom prefix is only allowed for Liquid Pack machine ID projects"}), 400
        try:
            prefix = int(prefix_field)
        except Exception:
            return jsonify({"error": "Prefix must be an integer"}), 400
        # Must be a positive integer
        if prefix <= 0:
            return jsonify({"error": "Machine ID prefix must be a positive integer"}), 400
        # Disallow prefixes within the auto-range for the division to avoid collisions.
        # For Liquid Pack the auto range is [2000, 3000).
        base_lp = 2000
        if base_lp <= prefix < base_lp + 1000:
            return jsonify({"error": "Machine ID prefix must not be between 2000 and 2999 (reserved for auto-generated projects)"}), 400
        # Ensure prefix is unique across all projects
        if Project.query.filter_by(prefix=prefix).first():
            return jsonify({"error": f"Prefix {prefix} already exists"}), 400
    # Determine prefix (auto or custom)
    if prefix is None:
        prefix = next_project_prefix(division)
    p = Project(title=title, prefix=prefix, division=division, is_active=True)
    db.session.add(p)
    db.session.commit()
    return jsonify(p.as_dict()), 201


@app.route("/api/projects/<int:pid>", methods=["DELETE", "PUT"])
@login_required
def api_project_update(pid: int):
    p = Project.query.get_or_404(pid)
    if request.method == "DELETE":
        # Soft delete
        p.is_active = False
        db.session.commit()
        return ("", 204)
    else:  # PUT
        data = request.get_json() or {}
        title = data.get("title")
        is_active = data.get("is_active")
        division = data.get("division")
        if title is not None:
            p.title = title.strip()
        if division is not None:
            if division not in VALID_DIVISIONS:
                return jsonify({"error": f"division must be one of {VALID_DIVISIONS}"}), 400
            p.division = division
        if is_active is not None:
            p.is_active = bool(is_active)
        db.session.commit()
        return jsonify(p.as_dict())

# ----------------------------------------------------------------------------
# API — Time Entries
# ----------------------------------------------------------------------------
@app.route("/api/entries", methods=["GET", "POST"])
@login_required
def api_entries():
    if request.method == "GET":
        q = TimeEntry.query.order_by(TimeEntry.start_time.desc())
        project_id = request.args.get("project_id")
        user_id = request.args.get("user_id")
        start = request.args.get("start")  # YYYY-MM-DD
        end = request.args.get("end")      # YYYY-MM-DD
        if project_id:
            q = q.filter(TimeEntry.project_id == int(project_id))
        if user_id:
            q = q.filter(TimeEntry.user_id == int(user_id))
        if start:
            try:
                start_dt = datetime.fromisoformat(start + "T00:00")
                q = q.filter(TimeEntry.start_time >= start_dt)
            except Exception:
                return jsonify({"error": "Invalid start date"}), 400
        if end:
            try:
                end_dt = datetime.fromisoformat(end + "T23:59:59")
                q = q.filter(TimeEntry.start_time <= end_dt)
            except Exception:
                return jsonify({"error": "Invalid end date"}), 400
        entries = q.all()
        return jsonify([e.as_dict() for e in entries])

    data = request.get_json() or request.form
    try:
        project_id = int(data.get("project_id"))
        start_time = parse_dt(data.get("start_time"))      # YYYY-MM-DDTHH:MM (raw)
        end_time = parse_dt(data.get("end_time"))          # YYYY-MM-DDTHH:MM (raw)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    travel_morning = bool(data.get("travel_morning"))
    travel_afternoon = bool(data.get("travel_afternoon"))

    # Apply travel shifts to what is saved
    if travel_morning:
        start_time = start_time - timedelta(hours=1)
    if travel_afternoon:
        end_time = end_time + timedelta(hours=1)

    if end_time <= start_time:
        return jsonify({"error": "End time must be after start time"}), 400

    notes = (data.get("notes") or "").strip()
    entry = TimeEntry(
        user_id=current_user.id,
        project_id=project_id,
        start_time=start_time,
        end_time=end_time,
        notes=notes,
        travel_morning=travel_morning,
        travel_afternoon=travel_afternoon,
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify(entry.as_dict()), 201


@app.route("/api/entries/<int:eid>", methods=["PUT", "DELETE"])
@login_required
def api_entry_update(eid: int):
    entry = TimeEntry.query.get_or_404(eid)
    if request.method == "DELETE":
        db.session.delete(entry)
        db.session.commit()
        return ("", 204)

    data = request.get_json() or {}

    if "project_id" in data:
        entry.project_id = int(data["project_id"])
    if "notes" in data:
        entry.notes = (data["notes"] or "").strip()

    # Update travel flags first (so we can use them when shifting)
    if "travel_morning" in data:
        entry.travel_morning = bool(data["travel_morning"])
    if "travel_afternoon" in data:
        entry.travel_afternoon = bool(data["travel_afternoon"])

    # Treat incoming times as raw user selections (unshifted), then re-apply shift
    raw_start = parse_dt(data["start_time"]) if "start_time" in data else None
    raw_end   = parse_dt(data["end_time"])   if "end_time" in data else None
    st = entry.start_time if raw_start is None else raw_start
    en = entry.end_time   if raw_end   is None else raw_end

    # Re-apply travel shift to saved values
    if entry.travel_morning:
        st = st - timedelta(hours=1)
    if entry.travel_afternoon:
        en = en + timedelta(hours=1)

    if en <= st:
        return jsonify({"error": "End time must be after start time"}), 400

    entry.start_time = st
    entry.end_time = en
    db.session.commit()
    return jsonify(entry.as_dict())

# ----------------------------------------------------------------------------
# API — Export CSV
# ----------------------------------------------------------------------------
@app.route("/api/export", methods=["GET"])
@login_required
def api_export():
    project_id = request.args.get("project_id")
    q = TimeEntry.query
    if project_id:
        q = q.filter(TimeEntry.project_id == int(project_id))
    q = q.order_by(TimeEntry.start_time.asc())

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "entry_id",
        "username",
        "project_prefix",
        "project_title",
        "project_division",
        "start_time",
        "end_time",
        "duration_hours",
        "notes",
        "travel_morning",
        "travel_afternoon",
    ])
    for e in q.all():
        writer.writerow([
            e.id,
            e.user.username if e.user else "",
            e.project.prefix if e.project else "",
            e.project.title if e.project else "",
            e.project.division if e.project else "",
            e.start_time.isoformat(sep=" ", timespec="minutes"),
            e.end_time.isoformat(sep=" ", timespec="minutes"),
            f"{e.duration_hours():.3f}",
            e.notes or "",
            int(e.travel_morning),
            int(e.travel_afternoon),
        ])

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="time_entries.csv")

# ----------------------------------------------------------------------------
# Minimal HTML (Tailwind + vanilla JS)
# ----------------------------------------------------------------------------
LOGIN_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
  <title>Login • Timesheet</title>
</head>
<body class="min-h-screen bg-slate-50 flex items-center justify-center p-6">
  <form method="POST" class="w-full max-w-sm bg-white p-6 rounded-2xl shadow">
    <h1 class="text-2xl font-semibold mb-4">Sign in</h1>
    {% if error %}
      <div class="mb-3 text-sm text-red-600">{{ error }}</div>
    {% endif %}
    <label class="block mb-2 text-sm">Username</label>
    <input name="username" class="w-full border rounded px-3 py-2 mb-4" placeholder="admin" required>
    <label class="block mb-2 text-sm">Password</label>
    <input name="password" type="password" class="w-full border rounded px-3 py-2 mb-6" placeholder="••••••" required>
    <button class="w-full bg-black text-white rounded-xl py-2">Login</button>
  </form>
</body>
</html>
"""

NAV_LINKS = """
  <nav class=\"ml-auto flex items-center gap-4\">
    <a class=\"text-sm underline\" href=\"/app\">Add Time</a>
    <a class=\"text-sm underline\" href=\"/projects\">Projects</a>
    <a class=\"text-sm underline\" href=\"/review\">Review</a>
    <a class=\"text-sm underline\" href=\"/admin\">Admin</a>
    <form method=\"POST\" action=\"/logout\">
      <button class=\"text-sm text-slate-600 underline\">Logout</button>
    </form>
  </nav>
"""

APP_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
  <title>Timesheet</title>
</head>
<body class="min-h-screen bg-slate-50">
  <header class="bg-white border-b sticky top-0 z-10">
    <div class="max-w-6xl mx-auto px-4 py-3 flex items-center gap-4">
      <h1 class="text-xl font-semibold">Timesheet</h1>
      {{ nav|safe }}
    </div>
  </header>

  <main class="max-w-6xl mx-auto p-4 grid grid-cols-1 gap-6">
    <!-- Time Entry Panel (full width) -->
    <section class="bg-white rounded-2xl shadow p-4">
      <h2 class="text-lg font-semibold mb-3">Add Time Entry</h2>
      <form id="entryForm" class="grid md:grid-cols-2 gap-3 items-end">
        <div class="flex gap-4 md:col-span-2">
          <label class="inline-flex items-center gap-1">
            <input id="filterMP" type="checkbox" class="border rounded">
            <span class="text-sm">MP</span>
          </label>
          <label class="inline-flex items-center gap-1">
            <input id="filterLP" type="checkbox" class="border rounded">
            <span class="text-sm">LP</span>
          </label>
        </div>
        <label class="block">
          <span class="text-sm">Project</span>
          <select id="entryProject" class="w-full border rounded px-3 py-2" required></select>
        </label>
        <label class="block">
          <span class="text-sm">Notes (optional)</span>
          <input id="entryNotes" class="w-full border rounded px-3 py-2" placeholder="e.g., design work">
        </label>
        <label class="block">
          <span class="text-sm">Date</span>
          <input id="entryDate" type="date" class="w-full border rounded px-3 py-2" required>
        </label>
        <div class="grid grid-cols-2 gap-3">
          <label class="block">
            <span class="text-sm">Start</span>
            <input id="entryStartTime" type="time" step="60" class="w-full border rounded px-3 py-2" required>
          </label>
          <label class="block">
            <span class="text-sm">End</span>
            <input id="entryEndTime" type="time" step="60" class="w-full border rounded px-3 py-2" required>
          </label>
        </div>

        <!-- Travel flags -->
        <div class="grid grid-cols-2 gap-3 md:col-span-2">
          <label class="inline-flex items-center gap-2">
            <input id="travelMorning" type="checkbox" class="border rounded">
            <span class="text-sm">Morning commute</span>
          </label>
          <label class="inline-flex items-center gap-2">
            <input id="travelAfternoon" type="checkbox" class="border rounded">
            <span class="text-sm">Afternoon commute</span>
          </label>
        </div>

        <div class="md:col-span-2 flex gap-2">
          <button class="bg-black text-white rounded-xl px-4 py-2" id="addEntryBtn">Save entry</button>
          <a id="exportCsv" class="ml-auto underline text-sm" href="#">Export CSV</a>
        </div>
      </form>
    </section>

    <!-- Recent Entries -->
    <section class="bg-white rounded-2xl shadow p-4">
      <h3 class="text-md font-semibold mb-2">Recent Entries</h3>
      <div class="overflow-x-auto">
        <table class="w-full text-sm" id="entriesTable">
          <thead>
            <tr class="text-left border-b">
              <th class="py-2">Project</th>
              <th>User</th>
              <th>Date</th>
              <th>Start</th>
              <th>End</th>
              <th>Hours</th>
              <th>Notes</th>
              <th></th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
  async function jsonFetch(url, opts={}) {
    const res = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, opts));
    if (!res.ok) {
      let msg = res.statusText;
      try { const j = await res.json(); msg = j.error || JSON.stringify(j) } catch {}
      throw new Error(msg);
    }
    return res.json();
  }

  function yyyy_mm_dd(d) {
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
  }

  function combineISO(dateStr, timeStr) {
    return `${dateStr}T${timeStr}`; // YYYY-MM-DDTHH:MM
  }

  // Populate project select only (no management here)
  // Store all projects for dynamic filtering in Add Time page
  let projectData = [];

  // Fetch projects once and populate the select based on filter checkboxes
  async function loadProjectsForSelect() {
    projectData = await jsonFetch('/api/projects');
    renderProjectSelect();
  }

  // Render the project select options based on MP/LP checkbox filters
  function renderProjectSelect() {
    const select = document.getElementById('entryProject');
    if (!select) return;
    select.innerHTML = '';
    const mpCheckbox = document.getElementById('filterMP');
    const lpCheckbox = document.getElementById('filterLP');
    const showMP = mpCheckbox ? mpCheckbox.checked : false;
    const showLP = lpCheckbox ? lpCheckbox.checked : false;
    const showMPActual = showMP || (!showMP && !showLP);
    const showLPActual = showLP || (!showMP && !showLP);

    const mpGroup = document.createElement('optgroup');
    mpGroup.label = 'Melbourne Power';
    const lpGroup = document.createElement('optgroup');
    lpGroup.label = 'Liquid Pack';

    for (const p of projectData) {
      if (!p.is_active) continue;
      if (p.division === 'Melbourne Power' && !showMPActual) continue;
      if (p.division === 'Liquid Pack' && !showLPActual) continue;
      const opt = document.createElement('option');
      opt.value = p.id;
      // Add a '#' to the prefix for Liquid Pack machine ID projects (prefix outside 2000-2999)
      let displayPrefix = p.prefix;
      if (p.division === 'Liquid Pack') {
        if (p.prefix < 2000 || p.prefix >= 3000) {
          displayPrefix = `#${p.prefix}`;
        }
      }
      opt.textContent = `${displayPrefix} — ${p.title}`;
      (p.division === 'Melbourne Power' ? mpGroup : lpGroup).appendChild(opt);
    }
    if (mpGroup.children.length) select.appendChild(mpGroup);
    if (lpGroup.children.length) select.appendChild(lpGroup);
  }

  function setDefaultDateAndTimes() {
    const today = new Date();
    document.getElementById('entryDate').value = yyyy_mm_dd(today);
    document.getElementById('entryStartTime').value = '06:00';
    // Default end time changed from 16:00 to 14:30 per new requirements
    document.getElementById('entryEndTime').value = '14:30';
    document.getElementById('travelMorning').checked = false;
    document.getElementById('travelAfternoon').checked = false;
  }

  async function loadEntries() {
    const data = await jsonFetch('/api/entries');
    const tbody = document.querySelector('#entriesTable tbody');
    tbody.innerHTML = '';
    for (const e of data) {
      const s = new Date(e.start_time);
      const ed = new Date(e.end_time);
      const pad = (n) => String(n).padStart(2, '0');
      const dateStr = `${s.getFullYear()}-${pad(s.getMonth()+1)}-${pad(s.getDate())}`;
      const stStr = `${pad(s.getHours())}:${pad(s.getMinutes())}`;
      const enStr = `${pad(ed.getHours())}:${pad(ed.getMinutes())}`;

      const tr = document.createElement('tr');
      tr.className = 'border-b';
      tr.innerHTML = `
        <td class='py-2'>${e.project_prefix} — ${e.project_title}</td>
        <td>${e.creator_username || ''}</td>
        <td>${dateStr}</td>
        <td>${stStr}</td>
        <td>${enStr}</td>
        <td>${e.duration_hours.toFixed(2)}</td>
        <td>${e.notes || ''}</td>
        <td class='text-right'>
          <button class='text-xs underline text-blue-600' data-edit='${e.id}'>Edit</button>
          <button class='text-xs underline text-red-600 ml-2' data-del='${e.id}'>Delete</button>
        </td>`;

      tr.querySelector('[data-del]').addEventListener('click', async (btn) => {
        const id = btn.target.getAttribute('data-del');
        if (!confirm('Delete this entry?')) return;
        await fetch(`/api/entries/${id}`, {method: 'DELETE'});
        await loadEntries();
      });

      tr.querySelector('[data-edit]').addEventListener('click', () => openEditModal(e));

      tbody.appendChild(tr);
    }
  }

  document.getElementById('entryForm').addEventListener('submit', (e) => e.preventDefault());

  document.getElementById('addEntryBtn').addEventListener('click', async (e) => {
    e.preventDefault();
    try {
      const project_id = document.getElementById('entryProject').value;
      const notes = document.getElementById('entryNotes').value;
      const dateStr = document.getElementById('entryDate').value;     // YYYY-MM-DD
      const start_t = document.getElementById('entryStartTime').value; // HH:MM
      const end_t = document.getElementById('entryEndTime').value;     // HH:MM
      const travel_morning = document.getElementById('travelMorning').checked;
      const travel_afternoon = document.getElementById('travelAfternoon').checked;

      const start_time = combineISO(dateStr, start_t); // raw
      const end_time = combineISO(dateStr, end_t);     // raw

      await jsonFetch('/api/entries', {
        method: 'POST',
        body: JSON.stringify({
          project_id, notes,
          start_time, end_time,   // raw from user inputs
          travel_morning, travel_afternoon
        })
      });
      document.getElementById('entryNotes').value = '';
      setDefaultDateAndTimes();
      await loadEntries();
    } catch (err) { alert(err.message); }
  });

  document.getElementById('exportCsv').addEventListener('click', (e) => {
    e.preventDefault();
    window.location = '/api/export';
  });

  // Edit modal (uses date picker & time-only inputs)
  function openEditModal(entry) {
    const overlay = document.createElement('div');
    overlay.className = 'fixed inset-0 bg-black/40 flex items-center justify-center p-4';
    overlay.innerHTML = `
      <div class='bg-white rounded-2xl shadow-xl p-4 w-full max-w-lg'>
        <h4 class='text-lg font-semibold mb-3'>Edit Entry #${entry.id} <span class='text-sm text-slate-500'>(by ${entry.creator_username || '—'})</span></h4>
        <div class='grid grid-cols-1 md:grid-cols-2 gap-3'>
          <label class='block md:col-span-2'>
            <span class='text-sm'>Project</span>
            <select id='editProject' class='w-full border rounded px-3 py-2'></select>
          </label>
          <label class='block md:col-span-2'>
            <span class='text-sm'>Notes</span>
            <input id='editNotes' class='w-full border rounded px-3 py-2'>
          </label>
          <label class='block'>
            <span class='text-sm'>Date</span>
            <input id='editDate' type='date' class='w-full border rounded px-3 py-2'>
          </label>
          <div class='grid grid-cols-2 gap-3'>
            <label class='block'>
              <span class='text-sm'>Start</span>
              <input id='editStartTime' type='time' step='60' class='w-full border rounded px-3 py-2'>
            </label>
            <label class='block'>
              <span class='text-sm'>End</span>
              <input id='editEndTime' type='time' step='60' class='w-full border rounded px-3 py-2'>
            </label>
          </div>
          <div class='grid grid-cols-2 gap-3 md:col-span-2'>
            <label class='inline-flex items-center gap-2'>
              <input id='editTravelMorning' type='checkbox' class='border rounded'>
              <span class='text-sm'>Morning commute</span>
            </label>
            <label class='inline-flex items-center gap-2'>
              <input id='editTravelAfternoon' type='checkbox' class='border rounded'>
              <span class='text-sm'>Afternoon commute</span>
            </label>
          </div>
        </div>
        <div class='mt-4 flex gap-2 justify-end'>
          <button id='cancelModal' class='px-4 py-2 rounded-xl border'>Cancel</button>
          <button id='saveModal' class='px-4 py-2 rounded-xl bg-black text-white'>Save</button>
        </div>
      </div>`;

    document.body.appendChild(overlay);

    (async () => {
      const projects = await jsonFetch('/api/projects');
      const sel = overlay.querySelector('#editProject');
      const mpGroup = document.createElement('optgroup'); mpGroup.label = 'Melbourne Power';
      const lpGroup = document.createElement('optgroup'); lpGroup.label = 'Liquid Pack';
      for (const p of projects) {
        if (!p.is_active && p.id !== entry.project_id) continue;
        const opt = document.createElement('option');
        opt.value = p.id; opt.textContent = `${p.prefix} — ${p.title}`;
        if (p.id === entry.project_id) opt.selected = true;
        (p.division === 'Melbourne Power' ? mpGroup : lpGroup).appendChild(opt);
      }
      sel.appendChild(mpGroup); sel.appendChild(lpGroup);

      overlay.querySelector('#editNotes').value = entry.notes || '';

      // Display raw times (unshifted) derived from stored values
      const sStored = new Date(entry.start_time);
      const eStored = new Date(entry.end_time);
      const sRaw = new Date(sStored.getTime() + (entry.travel_morning ? 60*60*1000 : 0));
      const eRaw = new Date(eStored.getTime() - (entry.travel_afternoon ? 60*60*1000 : 0));
      const pad = (n) => String(n).padStart(2, '0');
      overlay.querySelector('#editDate').value = `${sRaw.getFullYear()}-${pad(sRaw.getMonth()+1)}-${pad(sRaw.getDate())}`;
      overlay.querySelector('#editStartTime').value = `${pad(sRaw.getHours())}:${pad(sRaw.getMinutes())}`;
      overlay.querySelector('#editEndTime').value = `${pad(eRaw.getHours())}:${pad(eRaw.getMinutes())}`;

      overlay.querySelector('#editTravelMorning').checked = !!entry.travel_morning;
      overlay.querySelector('#editTravelAfternoon').checked = !!entry.travel_afternoon;
    })();

    overlay.querySelector('#cancelModal').onclick = () => overlay.remove();
    overlay.querySelector('#saveModal').onclick = async () => {
      try {
        const dateStr = overlay.querySelector('#editDate').value;
        const st = overlay.querySelector('#editStartTime').value;
        const en = overlay.querySelector('#editEndTime').value;
        const body = {
          project_id: Number(overlay.querySelector('#editProject').value),
          notes: overlay.querySelector('#editNotes').value,
          start_time: `${dateStr}T${st}`,  // raw
          end_time: `${dateStr}T${en}`,    // raw
          travel_morning: overlay.querySelector('#editTravelMorning').checked,
          travel_afternoon: overlay.querySelector('#editTravelAfternoon').checked,
        };
        await jsonFetch(`/api/entries/${entry.id}`, {method: 'PUT', body: JSON.stringify(body)});
        overlay.remove();
        await loadEntries();
      } catch (err) { alert(err.message); }
    };
  }

  // Init
  (function init(){
    // inject nav HTML from server
    // attach filter change events for project select in Add Time page
    const mpChk = document.getElementById('filterMP');
    if (mpChk) mpChk.addEventListener('change', renderProjectSelect);
    const lpChk = document.getElementById('filterLP');
    if (lpChk) lpChk.addEventListener('change', renderProjectSelect);
  })();
  loadProjectsForSelect().then(() => {
    setDefaultDateAndTimes();
    loadEntries();
  });
  </script>
</body>
</html>
"""

# Projects management page
PROJECTS_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
  <title>Projects • Timesheet</title>
</head>
<body class="min-h-screen bg-slate-50">
  <header class="bg-white border-b sticky top-0 z-10">
    <div class="max-w-6xl mx-auto px-4 py-3 flex items-center gap-4">
      <h1 class="text-xl font-semibold">Projects</h1>
      {{ nav|safe }}
    </div>
  </header>

  <main class="max-w-6xl mx-auto p-4 grid grid-cols-1 md:grid-cols-2 gap-6">
    <!-- Melbourne Power -->
    <section class="bg-white rounded-2xl shadow p-4">
      <h2 class="text-lg font-semibold mb-3">Melbourne Power</h2>
      <!-- Search filter for Melbourne Power projects -->
      <input id="searchMP" type="text" class="w-full border rounded px-2 py-1 mb-3 text-sm" placeholder="Search by prefix or title...">
      <form id="newProjectFormMP" class="flex gap-2 mb-3">
        <input id="projectTitleMP" class="flex-1 border rounded px-3 py-2" placeholder="New project title" required>
        <button class="bg-black text-white rounded-xl px-3">Add</button>
      </form>
      <!-- Two-column list of Melbourne Power projects -->
      <ul id="projectListMP" class="grid grid-cols-1 md:grid-cols-2 gap-1 text-sm"></ul>
    </section>

    <!-- Liquid Pack -->
    <section class="bg-white rounded-2xl shadow p-4">
      <!-- Heading with Machine ID toggle -->
      <h2 class="text-lg font-semibold mb-3 flex items-center gap-2">
        Liquid Pack
        <label class="inline-flex items-center gap-1 ml-auto text-sm">
          <input id="machineIdToggle" type="checkbox" class="border rounded">
          <span>Machine ID</span>
        </label>
      </h2>
      <!-- Search filter for Liquid Pack projects -->
      <input id="searchLP" type="text" class="w-full border rounded px-2 py-1 mb-3 text-sm" placeholder="Search by prefix or title...">
      <!-- Form for adding Liquid Pack projects or Machine ID projects -->
      <form id="newProjectFormLP" class="flex gap-2 mb-3">
        <!-- Normal project title input -->
        <div id="lpNormalFields" class="flex flex-1 gap-2">
          <input id="projectTitleLP" class="flex-1 border rounded px-3 py-2" placeholder="New project title" required>
        </div>
        <!-- Machine ID prefix + title inputs, hidden by default -->
        <div id="lpMachineFields" class="flex flex-1 gap-2 hidden">
          <input id="machinePrefixLP" class="w-24 border rounded px-3 py-2" placeholder="Prefix" pattern="\d*" title="Numeric prefix">
          <input id="machineTitleLP" class="flex-1 border rounded px-3 py-2" placeholder="Machine ID title" required>
        </div>
        <button class="bg-black text-white rounded-xl px-3">Add</button>
      </form>
      <!-- Two-column list of Liquid Pack projects -->
      <ul id="projectListLP" class="grid grid-cols-1 md:grid-cols-2 gap-1 text-sm"></ul>
    </section>
  </main>

  <script>
  async function jsonFetch(url, opts={}) {
    const res = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, opts));
    if (!res.ok) {
      let msg = res.statusText;
      try { const j = await res.json(); msg = j.error || JSON.stringify(j) } catch {}
      throw new Error(msg);
    }
    return res.json();
  }

  async function loadProjects() {
    const data = await jsonFetch('/api/projects');
    const listMP = document.getElementById('projectListMP');
    const listLP = document.getElementById('projectListLP');
    listMP.innerHTML = '';
    listLP.innerHTML = '';

    for (const p of data) {
      if (!p.is_active) continue;
      const li = document.createElement('li');
      li.className = 'flex justify-between items-center border rounded px-3 py-2';
      // Prefix span
      const prefixSpan = document.createElement('span');
      prefixSpan.className = 'font-medium';
      // Show a '#' prefix for Liquid Pack machine ID projects
      let displayPrefix = p.prefix;
      if (p.division === 'Liquid Pack') {
        // Auto-generated Liquid Pack projects fall within [2000,2999]; everything outside is a machine ID
        if (p.prefix < 2000 || p.prefix >= 3000) {
          displayPrefix = `#${p.prefix}`;
        }
      }
      prefixSpan.textContent = displayPrefix;
      li.appendChild(prefixSpan);
      // Title span with inline editing
      const titleSpan = document.createElement('span');
      titleSpan.className = 'flex-1 ml-2';
      titleSpan.textContent = p.title;
      titleSpan.style.cursor = 'pointer';
      titleSpan.addEventListener('click', () => {
        const input = document.createElement('input');
        input.type = 'text';
        input.value = p.title;
        input.className = 'flex-1 ml-2 border rounded px-2 py-1 text-sm';
        // Replace the span with input
        li.replaceChild(input, titleSpan);
        input.focus();
        input.select();
        const finishEdit = async (save) => {
          const newTitle = input.value.trim();
          if (save && newTitle && newTitle !== p.title) {
            try {
              await jsonFetch(`/api/projects/${p.id}`, {method: 'PUT', body: JSON.stringify({title: newTitle})});
            } catch (err) { alert(err.message); }
          }
          await loadProjects();
        };
        input.addEventListener('blur', () => finishEdit(true));
        input.addEventListener('keydown', async (ev) => {
          if (ev.key === 'Enter') { ev.preventDefault(); await finishEdit(true); }
          if (ev.key === 'Escape') { ev.preventDefault(); await finishEdit(false); }
        });
      });
      li.appendChild(titleSpan);
      // Remove button
      const delBtn = document.createElement('button');
      // Display a simple minus symbol instead of the word "Remove" to save space
      delBtn.className = 'text-red-600 text-xs';
      delBtn.setAttribute('data-id', p.id);
      delBtn.textContent = '−';
      delBtn.addEventListener('click', async (e) => {
        const id = e.target.getAttribute('data-id');
        if (!confirm('Mark this project inactive? Existing entries remain intact.')) return;
        await fetch(`/api/projects/${id}`, {method: 'DELETE'});
        await loadProjects();
      });
      li.appendChild(delBtn);
      if (p.division === 'Melbourne Power') {
        listMP.appendChild(li);
      } else {
        listLP.appendChild(li);
      }
    }

    // Apply active search filters after re-rendering lists
    if (typeof filterMPList === 'function') filterMPList();
    if (typeof filterLPList === 'function') filterLPList();
  }

  document.getElementById('newProjectFormMP').addEventListener('submit', async (e) => {
    e.preventDefault();
    const title = document.getElementById('projectTitleMP').value.trim();
    if (!title) return;
    try {
      await jsonFetch('/api/projects', {method: 'POST', body: JSON.stringify({title, division:'Melbourne Power'})});
      document.getElementById('projectTitleMP').value = '';
      await loadProjects();
    } catch (err) { alert(err.message); }
  });

  // Search/filter functions for projects lists
  function filterList(listId, query) {
    const list = document.getElementById(listId);
    if (!list) return;
    const term = (query || '').toString().toLowerCase();
    list.querySelectorAll('li').forEach(li => {
      const text = li.textContent.toLowerCase();
      li.style.display = text.includes(term) ? '' : 'none';
    });
  }
  function filterMPList() {
    const input = document.getElementById('searchMP');
    if (input) filterList('projectListMP', input.value);
  }
  function filterLPList() {
    const input = document.getElementById('searchLP');
    if (input) filterList('projectListLP', input.value);
  }
  // Attach input event listeners to search fields if they exist
  const searchMPInput = document.getElementById('searchMP');
  if (searchMPInput) searchMPInput.addEventListener('input', filterMPList);
  const searchLPInput = document.getElementById('searchLP');
  if (searchLPInput) searchLPInput.addEventListener('input', filterLPList);

  // Toggle display of Machine ID fields based on the checkbox
  const machineToggle = document.getElementById('machineIdToggle');
  
  if (machineToggle) {
    const normalDiv = document.getElementById('lpNormalFields');
    const machineDiv = document.getElementById('lpMachineFields');
    const titleInput = document.getElementById('projectTitleLP');
    const prefixInput = document.getElementById('machinePrefixLP');
    const machineTitleInput = document.getElementById('machineTitleLP');
    const updateVisibility = () => {
      if (machineToggle.checked) {
        // Show machine ID inputs, hide normal title input
        normalDiv.classList.add('hidden');
        machineDiv.classList.remove('hidden');
        // Switch HTML5 validation to the visible inputs
        if (titleInput) titleInput.removeAttribute('required');
        if (prefixInput) prefixInput.setAttribute('required','');
        if (machineTitleInput) machineTitleInput.setAttribute('required','');
      } else {
        normalDiv.classList.remove('hidden');
        machineDiv.classList.add('hidden');
        // Switch validation back to normal mode
        if (titleInput) titleInput.setAttribute('required','');
        if (prefixInput) prefixInput.removeAttribute('required');
        if (machineTitleInput) machineTitleInput.removeAttribute('required');
      }
    };
    machineToggle.addEventListener('change', updateVisibility);
    // Run once on load
    updateVisibility();
  }


  document.getElementById('newProjectFormLP').addEventListener('submit', async (e) => {
    e.preventDefault();
    const isMachine = document.getElementById('machineIdToggle')?.checked;
    if (isMachine) {
      // Create a Machine ID project with custom numeric prefix and title
      const prefixStr = document.getElementById('machinePrefixLP').value.trim();
      const title = document.getElementById('machineTitleLP').value.trim();
      if (!prefixStr || !title) {
        alert('Please enter both a prefix and a title for the machine ID project.');
        return;
      }
      const prefixNum = Number(prefixStr);
      if (Number.isNaN(prefixNum) || prefixNum <= 0) {
        alert('Prefix must be a positive number.');
        return;
      }
      if (prefixNum >= 2000 && prefixNum < 3000) {
        alert('Machine ID prefix must not be between 2000 and 2999, as that range is reserved for auto-generated Liquid Pack projects.');
        return;
      }
      try {
        await jsonFetch('/api/projects', {method: 'POST', body: JSON.stringify({title, division:'Liquid Pack', prefix: prefixNum})});
        // Clear fields
        document.getElementById('machinePrefixLP').value = '';
        document.getElementById('machineTitleLP').value = '';
        await loadProjects();
      } catch (err) { alert(err.message); }
    } else {
      // Create a normal Liquid Pack project (auto-prefix)
      const title = document.getElementById('projectTitleLP').value.trim();
      if (!title) return;
      try {
        await jsonFetch('/api/projects', {method: 'POST', body: JSON.stringify({title, division:'Liquid Pack'})});
        document.getElementById('projectTitleLP').value = '';
        await loadProjects();
      } catch (err) { alert(err.message); }
    }
  });

  loadProjects();
  </script>
</body>
</html>
"""

REVIEW_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
  <title>Review • Timesheet</title>
</head>
<body class="min-h-screen bg-slate-50">
  <header class="bg-white border-b sticky top-0 z-10">
    <div class="max-w-6xl mx-auto px-4 py-3 flex items-center gap-4">
      <h1 class="text-xl font-semibold">Review</h1>
      {{ nav|safe }}
    </div>
  </header>

  <main class="max-w-6xl mx-auto p-4 space-y-4">
    <section class="bg-white rounded-2xl shadow p-4">
      <div class="grid md:grid-cols-3 gap-3 items-end">
        <label class="block">
          <span class="text-sm">User</span>
          <select id="reviewUser" class="w-full border rounded px-3 py-2"></select>
        </label>
        <label class="block">
          <span class="text-sm">Week ending (Thursday)</span>
          <input id="reviewWeekEnd" type="date" class="w-full border rounded px-3 py-2">
        </label>
        <div class="flex items-end">
          <button id="reviewApply" class="bg-black text-white rounded-xl px-4 py-2">Apply</button>
        </div>
      </div>
    </section>

    <section class="bg-white rounded-2xl shadow p-4">
      <div class="overflow-x-auto">
        <table class="w-full text-sm table-fixed" id="reviewTable">
          <thead>
            <tr class="text-left border-b" id="reviewHead"></tr>
          </thead>
          <tbody id="reviewBody"></tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
  async function jsonFetch(url, opts={}) {
    const res = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, opts));
    if (!res.ok) {
      let msg = res.statusText;
      try { const j = await res.json(); msg = j.error || JSON.stringify(j) } catch {}
      throw new Error(msg);
    }
    return res.json();
  }

  function fmt(d){ const p=n=>String(n).padStart(2,'0'); return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`; }
  function label(d){ return d.toLocaleDateString(undefined,{weekday:'long', month:'short', day:'numeric'}); }

  // Current week ending Thursday
  function currentThursday(){
    const now = new Date();
    const day = now.getDay(); // 0 Sun .. 6 Sat
    const diffToThu = (4 - day + 7) % 7; // 4 = Thu
    const thu = new Date(now); thu.setDate(now.getDate() + diffToThu);
    thu.setHours(0,0,0,0);
    return thu;
  }

  function rangeFriToThu(thu){
    const days=[]; const d=new Date(thu);
    for(let i=6;i>=0;i--){ const x=new Date(d); x.setDate(d.getDate()-i); days.push(x); }
    return days; // Fri..Thu
  }

  async function loadUsers(){
    const users = await jsonFetch('/api/users');
    const sel = document.getElementById('reviewUser');
    sel.innerHTML='';
    for(const u of users){
      const opt=document.createElement('option'); opt.value=u.id; opt.textContent=u.username; sel.appendChild(opt);
    }
  }

  async function renderWeek(){
    const userId = document.getElementById('reviewUser').value;
    const weekEnd = new Date(document.getElementById('reviewWeekEnd').value);
    if(!userId || isNaN(weekEnd)) return;
    const days = rangeFriToThu(weekEnd);
    const start = new Date(days[0]); start.setHours(0,0,0,0);
    const end = new Date(days[6]); end.setHours(23,59,59,999);

    // Header Fri..Thu
    const head = document.getElementById('reviewHead');
    head.innerHTML = '';
    for(const d of days){
      const th=document.createElement('th'); th.className='py-2'; th.textContent = label(d); head.appendChild(th);
    }

    // Fetch entries for user & range
    const data = await jsonFetch(`/api/entries?user_id=${userId}&start=${fmt(start)}&end=${fmt(end)}`);

    // Group by day -> by project (sum hours)
    const byDay = Array.from({length:7},()=>new Map());
    const totals = new Array(7).fill(0);
    for(const e of data){
      const st = new Date(e.start_time);
      const idx = days.findIndex(d=> d.getFullYear()==st.getFullYear() && d.getMonth()==st.getMonth() && d.getDate()==st.getDate());
      if(idx<0) continue;
      const key = e.project_prefix; // prefix is unique
      const prev = byDay[idx].get(key) || {title: e.project_title, prefix: e.project_prefix, hours: 0};
      prev.hours += e.duration_hours;
      byDay[idx].set(key, prev);
      totals[idx] += e.duration_hours;
    }

    const body = document.getElementById('reviewBody');
    body.innerHTML = '';

    // Row 1: one row; each column = vertical stack of fixed-size square cards
    const tr = document.createElement('tr'); tr.className='align-top';
    for(let c=0;c<7;c++){
      const td=document.createElement('td'); td.className='py-3 align-top';
      const col=document.createElement('div');
      col.className='flex flex-col gap-3 items-start';
      for(const item of byDay[c].values()){
        const card=document.createElement('div');
        card.className='border rounded-2xl p-3 shadow-sm w-36 h-36 flex flex-col justify-between';
        card.innerHTML = `
          <div>
            <div class='font-bold text-base leading-tight'>${item.prefix}</div>
            <div class='text-slate-500 text-sm leading-tight'>${item.title}</div>
          </div>
          <div class='text-3xl font-semibold leading-none'>${item.hours.toFixed(2)} h</div>
        `;
        col.appendChild(card);
      }
      td.appendChild(col);
      tr.appendChild(td);
    }
    body.appendChild(tr);

    // Row 2: totals
    const trTot = document.createElement('tr'); trTot.className='border-t';
    for(let c=0;c<7;c++){
      const td=document.createElement('td'); td.className='py-2 font-semibold text-center';
      td.textContent = `Total: ${totals[c].toFixed(2)} h`;
      trTot.appendChild(td);
    }
    body.appendChild(trTot);
  }

  // Init
  (async function(){
    await loadUsers();
    const thu=currentThursday();
    document.getElementById('reviewWeekEnd').value = fmt(thu);
    document.getElementById('reviewApply').addEventListener('click', renderWeek);
    document.getElementById('reviewUser').addEventListener('change', renderWeek);
    renderWeek();
  })();
  </script>
</body>
</html>
"""


ADMIN_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.tailwindcss.com"></script>
  <title>Admin • Timesheet</title>
</head>
<body class="min-h-screen bg-slate-50">
  <header class="bg-white border-b sticky top-0 z-10">
    <div class="max-w-6xl mx-auto px-4 py-3 flex items-center gap-4">
      <h1 class="text-xl font-semibold">Admin</h1>
      {{ nav|safe }}
    </div>
  </header>

  <main class="max-w-6xl mx-auto p-4">
    <div class="bg-white rounded-2xl shadow p-4">
      <h2 class="text-lg font-semibold mb-3">All Entries (editable)</h2>
      <div class="overflow-x-auto">
        <table class="w-full text-sm" id="adminTable">
          <thead>
            <tr class="text-left border-b">
              <th class="py-2">ID</th>
              <th>User</th>
              <th>Project</th>
              <th>Date</th>
              <th>Start</th>
              <th>End</th>
              <th>Morning</th>
              <th>Afternoon</th>
              <th>Hours</th>
              <th>Notes</th>
              <th></th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
    <!-- Project Logbook: shows all projects ever created, including those marked inactive -->
    <div class="bg-white rounded-2xl shadow p-4 mt-6">
      <h2 class="text-lg font-semibold mb-3">All Projects (log)</h2>
      <div class="flex items-center gap-2 mb-3">
        <input id="projectSearch" type="text" placeholder="Search projects..." class="border rounded px-2 py-1 flex-grow">
        <select id="projectActiveFilter" class="border rounded px-2 py-1">
          <option value="">All</option>
          <option value="true">Active</option>
          <option value="false">Inactive</option>
        </select>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm" id="projectLogTable">
          <thead>
            <tr class="text-left border-b">
              <th class="py-2">ID</th>
              <th>Prefix</th>
              <th>Title</th>
              <th>Division</th>
              <th>Active</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </main>

  <script>
  async function jsonFetch(url, opts={}) {
    const res = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, opts));
    if (!res.ok) {
      let msg = res.statusText;
      try { const j = await res.json(); msg = j.error || JSON.stringify(j) } catch {}
      throw new Error(msg);
    }
    return res.json();
  }

  function toLocalDate(d){ const p=n=>String(n).padStart(2,'0'); return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`; }
  function toLocalTime(d){ const p=n=>String(n).padStart(2,'0'); return `${p(d.getHours())}:${p(d.getMinutes())}`; }

  async function loadAll() {
    const [projects, entries] = await Promise.all([
      jsonFetch('/api/projects'),
      jsonFetch('/api/entries')
    ]);
    const tbody = document.querySelector('#adminTable tbody');
    tbody.innerHTML = '';
    // Populate the project log table with all projects (including inactive ones).
    const projTbody = document.querySelector('#projectLogTable tbody');
    if (projTbody) {
      projTbody.innerHTML = '';
      for (const p of projects) {
        const ptr = document.createElement('tr');
        ptr.className = 'border-b';
        // Store active state on the row for filtering
        ptr.setAttribute('data-active', p.is_active ? 'true' : 'false');
        ptr.innerHTML = `
          <td class='py-2'>${p.id}</td>
          <td>${p.prefix}</td>
          <td>${p.title}</td>
          <td>${p.division}</td>
          <td>${p.is_active ? 'Yes' : 'No'}</td>
        `;
        projTbody.appendChild(ptr);
      }
    }
    for (const e of entries) {
      // Unshift to raw for admin editing
      const sStored = new Date(e.start_time);
      const enStored = new Date(e.end_time);
      const sRaw = new Date(sStored.getTime() + (e.travel_morning ? 60*60*1000 : 0));
      const enRaw = new Date(enStored.getTime() - (e.travel_afternoon ? 60*60*1000 : 0));

      const tr = document.createElement('tr');
      tr.className = 'border-b';
      tr.innerHTML = `
        <td class='py-2'>${e.id}</td>
        <td>${e.creator_username || ''}</td>
        <td>
          <select data-field='project_id' data-id='${e.id}' class='border rounded px-2 py-1'>
            ${projects.map(p => `<option value='${p.id}' ${p.id===e.project_id? 'selected':''}>${p.prefix} — ${p.title}</option>`).join('')}
          </select>
        </td>
        <td><input data-field='date' data-id='${e.id}' class='border rounded px-2 py-1' type='date'></td>
        <td><input data-field='start_only' data-id='${e.id}' class='border rounded px-2 py-1' type='time' step='60'></td>
        <td><input data-field='end_only' data-id='${e.id}' class='border rounded px-2 py-1' type='time' step='60'></td>
        <td class='text-center'><input type='checkbox' data-field='travel_morning' data-id='${e.id}'></td>
        <td class='text-center'><input type='checkbox' data-field='travel_afternoon' data-id='${e.id}'></td>
        <td>${e.duration_hours.toFixed(2)}</td>
        <td><input data-field='notes' data-id='${e.id}' class='border rounded px-2 py-1 w-full' value="${(e.notes||'').replace(/"/g,'&quot;')}"></td>
        <td class='text-right'><button class='text-xs underline text-red-600' data-del='${e.id}'>Delete</button></td>
      `;

      tr.querySelector("[data-field='date']").value = toLocalDate(sRaw);
      tr.querySelector("[data-field='start_only']").value = toLocalTime(sRaw);
      tr.querySelector("[data-field='end_only']").value = toLocalTime(enRaw);
      tr.querySelector("[data-field='travel_morning']").checked = !!e.travel_morning;
      tr.querySelector("[data-field='travel_afternoon']").checked = !!e.travel_afternoon;

      function gatherRowBody(row){
        const date = row.querySelector("[data-field='date']").value;
        const st = row.querySelector("[data-field='start_only']").value;
        const en = row.querySelector("[data-field='end_only']").value;
        return {
          project_id: Number(row.querySelector("[data-field='project_id']").value),
          start_time: `${date}T${st}`,
          end_time: `${date}T${en}`,
          notes: row.querySelector("[data-field='notes']").value,
          travel_morning: row.querySelector("[data-field='travel_morning']").checked,
          travel_afternoon: row.querySelector("[data-field='travel_afternoon']").checked,
        };
      }

      // Batch update per-row on any change
      tr.querySelectorAll('select, input').forEach(el => {
        el.addEventListener('change', async () => {
          const id = el.getAttribute('data-id');
          const body = gatherRowBody(tr);
          try {
            await jsonFetch(`/api/entries/${id}`, {method: 'PUT', body: JSON.stringify(body)});
            await loadAll();
          } catch (err) { alert(err.message); }
        });
      });

      tr.querySelector('[data-del]').addEventListener('click', async (btn) => {
        const id = btn.target.getAttribute('data-del');
        if (!confirm('Delete this entry?')) return;
        await fetch(`/api/entries/${id}`, {method: 'DELETE'});
        await loadAll();
      });

      tbody.appendChild(tr);
    }
  }

  // Filter functions for the project log table
  function applyProjectFilters() {
    const searchEl = document.getElementById('projectSearch');
    const activeEl = document.getElementById('projectActiveFilter');
    const search = searchEl ? searchEl.value.trim().toLowerCase() : '';
    const active = activeEl ? activeEl.value : '';
    document.querySelectorAll('#projectLogTable tbody tr').forEach(row => {
      const rowText = row.innerText.toLowerCase();
      const rowActive = row.getAttribute('data-active');
      const matchesSearch = !search || rowText.includes(search);
      let matchesActive = true;
      if (active === 'true') {
        matchesActive = rowActive === 'true';
      } else if (active === 'false') {
        matchesActive = rowActive === 'false';
      }
      row.style.display = (matchesSearch && matchesActive) ? '' : 'none';
    });
  }

  // Attach filter event listeners
  (function() {
    const searchEl = document.getElementById('projectSearch');
    if (searchEl) {
      searchEl.addEventListener('input', applyProjectFilters);
    }
    const activeEl = document.getElementById('projectActiveFilter');
    if (activeEl) {
      activeEl.addEventListener('change', applyProjectFilters);
    }
  })();

  loadAll();
  </script>
</body>
</html>
"""

# Utility to inject the common nav into templates
@app.context_processor
def inject_nav():
    return {"nav": NAV_LINKS}

if __name__ == "__main__":
    app.run(debug=True)

