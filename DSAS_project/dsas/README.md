# Digital Service Automation System (DSAS)

A Flask web application that **automates** digital service-request
handling with a configurable rules engine and a background scheduler —
instead of relying on an admin to manually triage every request.

## Core Concept: the Automation Rules Engine
Admins define rules of the form:

> **IF** `<field>` `<operator>` `<value>` **THEN** `<action>` `<action value>`

Rules attach to one of three triggers:
| Trigger | When it runs | Typical use |
|---|---|---|
| `on_create` | The instant a request is submitted | Auto-prioritize, auto-assign, notify |
| `on_status_change` | Whenever a request's status is changed | Follow-up notifications, re-routing |
| `scheduled` | Every 60s by a background job (APScheduler), or on-demand via "Run Automation Now" | SLA escalation, auto-close stale tickets |

Every action a rule performs is written to an **AutomationLog** (visible
per-request and in a global activity log) and creates a **Notification**
for the affected user — so automation is transparent and auditable, not
a black box.

### Default seeded rules
1. **Auto-prioritize Network Issues** — category = Network → priority = High
2. **Auto-assign Account & Access requests to Admin** — category = Account & Access → assign to admin@dsas.com
3. **Notify on Critical request creation** — priority = Critical → send notification
4. **SLA Escalation — Open over 24h** — age_hours > 24 → escalate priority one level
5. **Auto-close Resolved requests after 48h** — hours_since_resolved > 48 → status = Closed

Admins can add, activate/deactivate, or delete rules from the UI —
no code changes needed to change automation behavior.

## Setup

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000**.

Default admin (auto-seeded): `admin@dsas.com` / `admin123`
Register a normal account from the UI to try the requester flow.

## Project Structure
```
dsas/
├── app.py                     # models, rules engine, scheduler, routes
├── requirements.txt
├── dsas.db                     # created automatically on first run
├── templates/
│   ├── base.html
│   ├── index.html / login.html / register.html
│   ├── dashboard_user.html / dashboard_admin.html
│   ├── new_request.html / request_detail.html
│   ├── rules_list.html / new_rule.html
│   ├── automation_logs.html / notifications.html
│   └── error.html
└── static/css/style.css
```

## Seeing Automation in Action
- Create a request with category **Network** → watch its priority jump
  to **High** automatically, logged on the request's Automation Activity
  timeline.
- Create a request with priority **Critical** → check the 🔔 bell icon,
  you'll have a new notification instantly.
- As admin, go to a request and manually edit its `created_at` in the DB
  (or just wait 24h in a real deployment) then click **Run Automation
  Now** on the dashboard to see SLA escalation fire.
- Add your own rule at **Automation Rules → New Rule**, e.g. escalate
  Billing issues open more than 2 hours, using `condition_field=age_hours`,
  `condition_value=2`.

## Extending the Project
- Add more condition fields (e.g. `requester_email`) or compound
  AND/OR conditions
- Send real email/SMS in the `notify` action (e.g. via Flask-Mail/Twilio)
- Add a visual drag-and-drop rule builder
- Expose the rules engine as a REST API for external systems to submit
  requests into DSAS
- Persist scheduler run history / add per-rule run frequency
