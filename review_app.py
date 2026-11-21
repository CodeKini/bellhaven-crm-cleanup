#!/usr/bin/env python3
"""Local-only Bellhaven review tool. No CRM writes without an approval click.

Run: python3 review_app.py
Files are resolved relative to this script. Python standard library only.
"""
import getpass
import hashlib
import html
import json
import secrets
import threading
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
PROPOSALS = HERE / 'proposals.json'
DECISIONS = HERE / 'decisions.json'
API = 'https://analyst-assessment-production.up.railway.app/api/v1/accounts'
PARENT = '0015QAPLGS3FVYEEEM'
HOST, PORT = '127.0.0.1', 8000
LOCK = threading.RLock()
SESSION = secrets.token_urlsafe(32)
TOKEN = None


def website_key(site):
    if not site:
        return None
    # Physical address + city + state + ZIP: stable across name changes.
    parts = [str(site.get(field, '')).strip().casefold() for field in
             ('street_address', 'city', 'state', 'zip_code')]
    return 'site:' + hashlib.sha256(json.dumps(parts, separators=(',', ':')).encode()).hexdigest()[:24]


def proposal_key(p):
    if p['category'] in ('DUPLICATE', 'NO_LONGER_LISTED'):
        account = p['crm_account']
        return 'account:' + p['category'] + ':' + account['account_id']
    return website_key(p.get('website_location'))


def read_json(path, fallback):
    if not path.exists():
        return fallback
    with path.open(encoding='utf-8') as file:
        return json.load(file)


def save_decisions(data):
    temp = DECISIONS.with_name('decisions.json.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temp.replace(DECISIONS)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def request_api(method, account_id=None, fields=None):
    url = API + ('/' + account_id if account_id else '')
    payload = json.dumps(fields).encode('utf-8') if fields is not None else None
    req = Request(url, data=payload, method=method, headers={
        'Authorization': 'Bearer ' + TOKEN,
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    })
    try:
        with urlopen(req, timeout=25) as response:
            body = response.read(1000000).decode('utf-8', errors='replace')
            return json.loads(body) if body.strip() else {}
    except HTTPError as exc:
        detail = exc.read(2000).decode('utf-8', errors='replace')
        raise RuntimeError(f'CRM API HTTP {exc.code}: {detail}') from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f'CRM connection failed: {exc}') from exc


def created_id(result):
    if isinstance(result, dict):
        for candidate in (result, result.get('account'), result.get('data')):
            if isinstance(candidate, dict):
                value = candidate.get('account_id') or candidate.get('id')
                if isinstance(value, str) and value.strip():
                    return value.strip()
    raise RuntimeError('POST succeeded but API did not return an account_id. Stop and verify the CRM before retrying; a new account may already exist.')


def validate_and_plan(p):
    """Convert exact reviewed proposal actions into a safe single-account API plan."""
    category = p['category']
    actions = p.get('proposed_changes') or []
    if any(a['action'] == 'MANUAL_REVIEW' for a in actions):
        raise ValueError('Manual review required: no exact CRM action has been proposed. Reject this proposal or update it after resolving the ambiguity.')
    account = p.get('crm_account')
    if category == 'CONFIDENT_MATCH':
        if any(a['action'] != 'NO_CHANGE' for a in actions):
            raise ValueError('Unexpected changes in CONFIDENT_MATCH')
        return 'no_change', None, {}
    if category == 'CREATE_NEW' or p.get('chow'):
        creates = [a for a in actions if a['action'] == 'CREATE_ACCOUNT']
        if len(creates) != 1:
            raise ValueError('Expected exactly one CREATE_ACCOUNT instruction')
        create = creates[0]
        if create.get('care_type_review') or create['values'].get('care_type') is None:
            raise ValueError('Care type must be resolved manually before creating an account.')
        fields = {k: v for k, v in create['values'].items() if k in {
            'name', 'parent_id', 'billing_street', 'billing_city', 'billing_state',
            'billing_zip', 'care_type', 'status'}}
        if fields.get('parent_id') != PARENT:
            raise ValueError('New account must be under Bellhaven parent')
        if category == 'CREATE_NEW' and len(actions) != 1:
            raise ValueError('Unexpected extra actions on CREATE_NEW')
        if p.get('chow'):
            if category != 'NEEDS_FIX' or not account:
                raise ValueError('Invalid CHOW proposal')
            if not (float(account.get('lifetime_revenue') or 0) > 0 and float(account.get('outstanding_ar') or 0) > 0):
                raise ValueError('CHOW must have positive lifetime revenue AND outstanding AR')
            links = [a for a in actions if a['action'] == 'UPDATE_FIELD_AFTER_CREATION']
            if len(links) != 1 or links[0]['field'] != 'chow_current_account' or links[0]['account_id'] != account['account_id']:
                raise ValueError('Unsafe CHOW linking instruction')
            if account.get('chow_current_account'):
                raise ValueError('Old account already has a CHOW link; inspect manually')
        return ('chow' if p.get('chow') else 'create'), account['account_id'] if account else None, fields
    if not account:
        raise ValueError('No CRM account specified to patch')
    account_id = account['account_id']
    patch = {}
    for action in actions:
        if action['action'] == 'UPDATE_FIELD':
            if action['account_id'] != account_id:
                raise ValueError('Change targets an unexpected CRM account')
            patch[action['field']] = action['to']
        elif action['action'] == 'UPDATE_FIELDS':
            if action['account_id'] != account_id:
                raise ValueError('Change targets an unexpected CRM account')
            patch.update({field: values['to'] for field, values in action['fields'].items()})
        else:
            raise ValueError('Unsupported action: ' + action['action'])
    if category == 'DUPLICATE':
        survivor = p.get('surviving_crm_account') or {}
        if patch != {'duplicate_of_account': survivor.get('account_id'), 'status': 'Inactive'}:
            raise ValueError('Duplicate update must mark the loser Inactive and link survivor')
    if category == 'NO_LONGER_LISTED':
        if patch.get('status') != 'Needs Review' or 'note' not in patch:
            raise ValueError('Missing Needs Review status/note')
        original_note = account.get('note') or ''
        explanation = patch['note']
        patch['note'] = original_note if explanation in original_note else ('\n'.join(x for x in (original_note, explanation) if x))
    if category not in ('NEEDS_FIX', 'DUPLICATE', 'NO_LONGER_LISTED'):
        raise ValueError('Unsupported category')
    if 'parent_id' in patch and patch['parent_id'] != PARENT:
        raise ValueError('Only reparenting to Bellhaven is permitted')
    if 'parent_id' in patch and float(account.get('lifetime_revenue') or 0) > 0 and float(account.get('outstanding_ar') or 0) > 0:
        raise ValueError('CHOW protection: account with revenue and AR cannot be directly reparented')
    # parent_name is display information, not necessarily writable in Salesforce-like CRMs.
    patch.pop('parent_name', None)
    if not patch:
        raise ValueError('Empty PATCH')
    return 'patch', account_id, patch


def apply(p, state, key):
    mode, account_id, fields = validate_and_plan(p)
    if mode == 'no_change':
        return None
    if mode == 'patch':
        request_api('PATCH', account_id, fields)
        return {'patched_account_id': account_id}
    if mode == 'create':
        result = request_api('POST', fields=fields)
        new_id = created_id(result)
        return {'new_account_id': new_id}
    if mode == 'chow':
        progress = state.get(key, {})
        new_id = progress.get('new_account_id')
        if not new_id:
            result = request_api('POST', fields=fields)
            new_id = created_id(result)
            state[key] = {'status': 'in_progress', 'new_account_id': new_id,
                          'category': p['category'], 'updated_at': now()}
            save_decisions(state)  # Durable checkpoint BEFORE patching original.
        request_api('PATCH', account_id, {'chow_current_account': new_id})
        return {'new_account_id': new_id, 'linked_old_account_id': account_id}
    raise ValueError('Unknown mode')


def esc(value):
    return html.escape(str(value), quote=True)


def pretty(value):
    return esc(json.dumps(value, indent=2, ensure_ascii=False))


def render_page(proposals, state, message='', error=False):
    pending = [p for p in proposals if state.get(proposal_key(p), {}).get('status') not in ('approved', 'rejected')]
    decided = sum(d.get('status') in ('approved', 'rejected') for d in state.values())
    msg = f'<p class="notice {"error" if error else ""}">{esc(message)}</p>' if message else ''
    if pending:
        p = pending[0]
        key = proposal_key(p)
        name = (p.get('website_location') or p.get('crm_account') or {}).get('name', '(unnamed)')
        indicator = '<strong style="color:#9b251b">CHOW — create first, link old account; never reparent old</strong>' if p.get('chow') else ''
        progress = state.get(key) or {}
        checkpoint = f'<p>CHOW new account already created: <code>{esc(progress["new_account_id"])}</code>. Approving again retries only the old-account link.</p>' if progress.get('new_account_id') else ''
        buttons = f'''<form method="post" action="/decision" class="buttons">
<input type="hidden" name="key" value="{esc(key)}"><input type="hidden" name="csrf" value="{esc(SESSION)}">
<button name="choice" value="approve" class="approve">Approve and apply</button>
<button name="choice" value="reject" class="reject">Reject</button></form>'''
        detail = f'''<div class="panel"><p class="tag">{esc(p['category'])}</p><h2>{esc(name)}</h2>{indicator}{checkpoint}
<h3>Website location</h3><pre>{pretty(p.get('website_location'))}</pre>
<h3>Matched CRM account</h3><pre>{pretty(p.get('crm_account'))}</pre>
{('<h3>Possible CRM accounts</h3><pre>'+pretty(p['candidate_crm_accounts'])+'</pre>') if p.get('candidate_crm_accounts') else ''}
{('<h3>Surviving CRM account</h3><pre>'+pretty(p['surviving_crm_account'])+'</pre>') if p.get('surviving_crm_account') else ''}
<h3>Evidence</h3><pre>{pretty(p.get('evidence'))}</pre>
<h3>Issues</h3><pre>{pretty(p.get('issues'))}</pre>
<h3>Exact proposed changes</h3><pre>{pretty(p.get('proposed_changes'))}</pre>{buttons}</div>'''
    else:
        detail = '<div class="panel"><h2>No pending proposals</h2><p>All loaded proposals have been approved or rejected.</p></div>'
    counts = Counter(d.get('status') for d in state.values())
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bellhaven CRM review</title><style>
body{{font-family:system-ui,-apple-system,sans-serif;margin:24px auto;padding:0 18px;max-width:960px;color:#17212b;font-size:19px;line-height:1.5;background:#f8f9fb}}
h1{{font-size:36px;margin:0 0 6px}}h2{{font-size:29px}}h3{{font-size:23px;margin-bottom:6px}}
.panel{{background:white;border:1px solid #d3d9e1;border-radius:12px;padding:24px;margin-top:22px}}
.tag{{font-weight:bold;color:#1b4f91}}pre{{font-size:15px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere;background:#f0f3f7;padding:16px;border-radius:8px}}
button{{font-size:22px;font-weight:bold;border:0;padding:17px 25px;border-radius:8px;cursor:pointer}}
.approve{{background:#126c3f;color:white}}.reject{{background:#a42727;color:white}}.buttons{{display:flex;gap:12px;flex-wrap:wrap;margin-top:24px}}
.notice{{padding:14px;background:#e2f4e8;border:1px solid #83c19a;border-radius:8px}}.error{{background:#fff0ee;border-color:#e59287}}
small{{color:#555}}code{{overflow-wrap:anywhere}}
</style></head><body><h1>Bellhaven CRM Review</h1><p>Pending: <b>{len(pending)}</b> / {len(proposals)} loaded proposals | Decisions saved: {decided} (approved {counts['approved']}, rejected {counts['rejected']})</p>
<small>Only pressing Approve can send a CRM write. Reject saves a local decision only. Only local browser access is permitted.</small>{msg}{detail}</body></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_html(self, body, status=200):
        encoded = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(encoded)

    def local_origin_ok(self):
        origin = self.headers.get('Origin')
        if not origin:
            return True  # CSRF secret still required.
        return origin in (f'http://localhost:{PORT}', f'http://127.0.0.1:{PORT}')

    def do_GET(self):
        if urlsplit(self.path).path != '/':
            self.send_error(404)
            return
        with LOCK:
            self.send_html(render_page(self.server.proposals, read_json(DECISIONS, {})))

    def do_POST(self):
        if urlsplit(self.path).path != '/decision' or not self.local_origin_ok():
            self.send_error(403)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192:
                raise ValueError('Invalid request size')
            data = parse_qs(self.rfile.read(size).decode('utf-8'), strict_parsing=True)
            key = data['key'][0]
            choice = data['choice'][0]
            if data['csrf'][0] != SESSION or choice not in ('approve', 'reject'):
                raise ValueError('Invalid review form')
        except (ValueError, KeyError, IndexError):
            self.send_error(400, 'Invalid form request')
            return
        with LOCK:
            proposals = self.server.proposals
            found = next((p for p in proposals if proposal_key(p) == key), None)
            state = read_json(DECISIONS, {})
            if not found:
                self.send_html(render_page(proposals, state, 'Proposal was not found. Reload the page.', True), 400)
                return
            if state.get(key, {}).get('status') in ('approved', 'rejected'):
                self.send_html(render_page(proposals, state, 'Already decided. No action repeated.'))
                return
            if choice == 'reject':
                if state.get(key, {}).get('new_account_id'):
                    self.send_html(render_page(proposals, state, 'Cannot reject a CHOW after its new account was already created. Retry the link or resolve manually.', True), 409)
                    return
                state[key] = {'status': 'rejected', 'category': found['category'], 'facility': (found.get('website_location') or found.get('crm_account') or {}).get('name'), 'decided_at': now()}
                save_decisions(state)
                self.send_html(render_page(proposals, state, 'Rejected and saved. No API call made.'))
                return
            try:
                result = apply(found, state, key)
            except (ValueError, RuntimeError, TypeError, KeyError) as exc:
                # Do not set approved/rejected after failure; CHOW checkpoint is retained.
                self.send_html(render_page(proposals, read_json(DECISIONS, {}), f'Approval not completed: {exc}. You may retry after resolving this issue.', True), 400)
                return
            state[key] = {'status': 'approved', 'category': found['category'], 'facility': (found.get('website_location') or found.get('crm_account') or {}).get('name'), 'decided_at': now(), 'result': result}
            save_decisions(state)
            self.send_html(render_page(proposals, state, 'Approved and saved. ' + ('CRM API change applied.' if result else 'No CRM change needed.')))

    def log_message(self, fmt, *args):
        # Don't log request bodies or Authorization tokens.
        print('[local review]', fmt % args)


def main():
    global TOKEN
    doc = read_json(PROPOSALS, {})
    proposals = doc.get('proposals', [])
    if not isinstance(proposals, list):
        raise SystemExit('proposals.json must contain a proposals list')
    keys = [proposal_key(p) for p in proposals]
    if len(keys) != len(set(keys)):
        raise SystemExit('Proposal identity collision. Resolve duplicates before starting.')
    saved = read_json(DECISIONS, {})
    if not isinstance(saved, dict):
        raise SystemExit('decisions.json should be a JSON object')
    TOKEN = getpass.getpass('Paste CRM API token (hidden input): ').strip()
    if not TOKEN:
        raise SystemExit('No token entered. Nothing started.')
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.proposals = proposals
    print(f'\nReview app running at http://localhost:{PORT}')
    print(f'Loaded {len(proposals)} proposals. Decisions file: {DECISIONS}')
    print('Open this address in your browser. Press Control+C in Terminal to stop.')
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print('\nStopped. Your decisions remain in decisions.json.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
