#!/usr/bin/env python3
"""Create review-only Bellhaven CRM reconciliation proposals. No API requests or CRM writes."""
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEBSITE_FILE = HERE / 'bellhaven_locations.csv'
CRM_FILE = HERE / 'crm_accounts.json'
OUTPUT_FILE = HERE / 'proposals.json'
DECISIONS_FILE = HERE / 'decisions.json'
PARENT_ID = '0015QAPLGS3FVYEEEM'
PARENT_NAME = 'Bellhaven Senior Living (Parent Account)'



def website_key(site):
    if not site:
        return None
    parts = [str(site.get(field, '')).strip().casefold() for field in
             ('street_address', 'city', 'state', 'zip_code')]
    return 'site:' + hashlib.sha256(json.dumps(parts, separators=(',', ':')).encode()).hexdigest()[:24]


def proposal_key(p):
    if p['category'] in ('DUPLICATE', 'NO_LONGER_LISTED'):
        return 'account:' + p['category'] + ':' + p['crm_account']['account_id']
    return website_key(p.get('website_location'))


def decided_keys_and_accounts():
    if not DECISIONS_FILE.exists():
        return set(), set()
    with DECISIONS_FILE.open(encoding='utf-8') as file:
        decisions = json.load(file)
    if not isinstance(decisions, dict):
        raise SystemExit('decisions.json should be a JSON object')
    done = {key for key, item in decisions.items()
            if isinstance(item, dict) and item.get('status') in ('approved', 'rejected')}
    # For account-only proposals (DUPLICATE / NO_LONGER_LISTED), avoid emitting
    # another category for an already reviewed account after a CRM refresh.
    reviewed_accounts = {key.rsplit(':', 1)[1] for key in done if key.startswith('account:')}
    return done, reviewed_accounts

def normalize(value):
    """Case, whitespace and punctuation-insensitive comparison."""
    value = str(value or '').lower().strip().replace('&', ' and ')
    value = re.sub(r'[^a-z0-9]+', ' ', value)
    return ' '.join(value.split())


def address_key(value):
    """Normalize common street abbreviations; never replace original values."""
    words = normalize(value).split()
    replacements = {
        'street': 'st', 'road': 'rd', 'avenue': 'ave', 'lane': 'ln',
        'drive': 'dr', 'boulevard': 'blvd', 'court': 'ct', 'place': 'pl',
        'west': 'w', 'east': 'e', 'north': 'n', 'south': 's',
        'northwest': 'nw', 'northeast': 'ne', 'southwest': 'sw',
        'southeast': 'se', 'parkway': 'pkwy', 'pike': 'pk',
    }
    return ' '.join(replacements.get(word, word) for word in words)


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def money(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        raise ValueError(f'Invalid financial amount in CRM: {value!r}')


def care_values(offerings):
    """Translate website care labels into CRM vocabulary."""
    result = []
    for item in str(offerings or '').split(';'):
        value = normalize(item)
        if not value:
            continue
        if 'rehabilitation' in value or 'nursing' in value:
            mapped = 'Skilled Nursing'
        elif 'memory' in value:
            mapped = 'Memory Care'
        elif 'assisted living' in value:
            mapped = 'Assisted Living'
        elif 'independent living' in value:
            mapped = 'Independent Living'
        else:
            mapped = item.strip()
        if mapped not in result:
            result.append(mapped)
    return result


def evidence(site, account):
    ns, nc = normalize(site['name']), normalize(account['name'])
    city = normalize(site['city']) == normalize(account['billing_city'])
    state = normalize(site['state']) == normalize(account['billing_state'])
    zipcode = str(site['zip_code']).strip() == str(account['billing_zip']).strip()
    street = address_key(site['street_address']) == address_key(account['billing_street'])
    name_exact = ns == nc
    name_sim = similarity(ns, nc)
    street_sim = similarity(address_key(site['street_address']), address_key(account['billing_street']))
    return {
        'name': {'website': site['name'], 'crm': account['name'], 'normalized_match': name_exact, 'similarity': round(name_sim, 3)},
        'city': {'website': site['city'], 'crm': account['billing_city'], 'match': city},
        'state': {'website': site['state'], 'crm': account['billing_state'], 'match': state},
        'zip_code': {'website': site['zip_code'], 'crm': account['billing_zip'], 'match': zipcode},
        'street_address': {'website': site['street_address'], 'crm': account['billing_street'], 'normalized_match': street, 'similarity': round(street_sim, 3)},
        'care_type': {'website_offerings': site['care_offerings'], 'crm': account['care_type'], 'equivalent_website_types': care_values(site['care_offerings']), 'match': account['care_type'] in care_values(site['care_offerings'])},
        'parent_id': {'expected': PARENT_ID, 'crm': account['parent_id'], 'match': account['parent_id'] == PARENT_ID},
    }


def candidate_score(site, account):
    e = evidence(site, account)
    name_sim = e['name']['similarity']
    street_sim = e['street_address']['similarity']
    city = e['city']['match']
    zipcode = e['zip_code']['match']
    state = e['state']['match']
    street = e['street_address']['normalized_match']
    name_exact = e['name']['normalized_match']
    # Never match on name alone. Require city and either ZIP or strong name+street.
    if not city or not state:
        return None
    if not zipcode and not (name_exact and street):
        # Allow a clear name+city+street match despite a bad ZIP code.
        if not (name_sim >= 0.90 and street_sim >= 0.94):
            return None
    # Identical address + city + ZIP is useful even if the facility has been renamed.
    if not (name_sim >= 0.64 or street or street_sim >= 0.95):
        return None
    return (40 if zipcode else 0) + 15 + (30 * name_sim) + (25 if street else 20 * street_sim) + (4 if account['parent_id'] == PARENT_ID else 0)


def proposed_creation(site, account_placeholder):
    types = care_values(site['care_offerings'])
    return {
        'action': 'CREATE_ACCOUNT',
        'new_account_placeholder': account_placeholder,
        'values': {
            'name': site['name'], 'parent_id': PARENT_ID, 'parent_name': PARENT_NAME,
            'billing_street': site['street_address'], 'billing_city': site['city'],
            'billing_state': site['state'], 'billing_zip': site['zip_code'],
            'care_type': types[0] if len(types) == 1 else None,
            'status': 'Active',
        },
        'care_type_review': ('Website lists multiple offerings; CRM care_type is singular. Select a primary type manually.' if len(types) > 1 else None),
    }


def main():
    with WEBSITE_FILE.open(newline='', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file)
        expected = ['name', 'street_address', 'city', 'state', 'zip_code', 'care_offerings']
        if reader.fieldnames != expected:
            raise SystemExit(f'Unexpected website columns: {reader.fieldnames}')
        sites = list(reader)
    with CRM_FILE.open(encoding='utf-8') as file:
        accounts = json.load(file)
    if not isinstance(accounts, list) or not all(isinstance(a, dict) for a in accounts):
        raise SystemExit('CRM JSON must contain a list of account objects.')
    ids = [a['account_id'] for a in accounts]
    if len(ids) != len(set(ids)):
        raise SystemExit('CRM has repeated account IDs; manual review needed.')

    decided_keys, reviewed_accounts = decided_keys_and_accounts()
    proposals = []
    used_ids = set()
    assigned = defaultdict(list)
    for position, site in enumerate(sites, 1):
        candidates = sorted(
            ((candidate_score(site, account), account) for account in accounts),
            key=lambda pair: (-1 if pair[0] is None else pair[0]), reverse=True,
        )
        candidates = [(score, acct) for score, acct in candidates if score is not None]
        # Same-facility duplicate candidates can have similarly high scores.
        # Prefer closest name/address and stable account ID; don't silently decide
        # between different business names sharing an address.
        top = candidates[0] if candidates else None
        if top:
            top_score = top[0]
            close = [a for score, a in candidates if top_score - score <= 7]
            different_names = {normalize(a['name']) for a in close}
            ambiguous = len(different_names) > 1 and not any(
                normalize(a['name']) == normalize(site['name']) for a in close
            )
            if ambiguous:
                # One website proposal, but no risky account mutation before review.
                proposals.append({
                    'category': 'NEEDS_FIX', 'website_location': site,
                    'crm_account': None,
                    'candidate_crm_accounts': close,
                    'evidence': {'reason': 'Multiple plausible accounts at the same location; ownership cannot be determined safely.',
                                 'candidates': [{'account_id': a['account_id'], 'score': round(s, 2), 'details': evidence(site, a)} for s, a in candidates if a in close]},
                    'issues': ['AMBIGUOUS_MATCH_MANUAL_REVIEW'],
                    'proposed_changes': [{'action': 'MANUAL_REVIEW', 'details': 'Choose the correct existing account or confirm whether to create a new account; no CRM change is proposed yet.'}],
                    'requires_review': True,
                    'chow': False,
                })
                continue
            # Avoid pairing two distinct website locations with the same account.
            matching = [(score, a) for score, a in candidates if a['account_id'] not in used_ids]
            if not matching:
                top = None
            else:
                top = matching[0]
        if not top:
            placeholder = f'NEW_BELLHAVEN_{position:03d}'
            proposals.append({
                'category': 'CREATE_NEW', 'website_location': site, 'crm_account': None,
                'evidence': {'reason': 'No sufficiently supported CRM match from name, city, ZIP, and street address.',
                             'candidate_count': len(candidates)},
                'issues': ['ACCOUNT_MISSING'],
                'proposed_changes': [proposed_creation(site, placeholder)],
                'requires_review': True, 'chow': False,
            })
            continue

        score, account = top
        used_ids.add(account['account_id'])
        assigned[account['account_id']].append(position)
        e = evidence(site, account)
        changes, issues = [], []
        # Preserve only substantive name differences; punctuation-only changes are equal.
        if not e['name']['normalized_match']:
            issues.append('OUTDATED_NAME')
            changes.append({'action': 'UPDATE_FIELD', 'account_id': account['account_id'], 'field': 'name', 'from': account['name'], 'to': site['name']})
        for source, target in [('street_address', 'billing_street'), ('city', 'billing_city'), ('state', 'billing_state'), ('zip_code', 'billing_zip')]:
            matched = e[source].get('normalized_match', e[source].get('match'))
            if not matched:
                issues.append('DIFFERENT_ADDRESS')
                changes.append({'action': 'UPDATE_FIELD', 'account_id': account['account_id'], 'field': target, 'from': account[target], 'to': site[source]})
        if not e['care_type']['match']:
            issues.append('DIFFERENT_CARE_TYPE')
            types = care_values(site['care_offerings'])
            if len(types) == 1:
                changes.append({'action': 'UPDATE_FIELD', 'account_id': account['account_id'], 'field': 'care_type', 'from': account['care_type'], 'to': types[0]})
            else:
                changes.append({'action': 'MANUAL_REVIEW', 'field': 'care_type', 'existing': account['care_type'], 'website_care_options': types, 'reason': 'CRM care_type holds one value but website lists multiple offerings.'})
        chow = False
        if account['parent_id'] != PARENT_ID:
            issues.append('MISSING_PARENT' if not account['parent_id'] else 'WRONG_PARENT')
            if money(account['lifetime_revenue']) > 0 and money(account['outstanding_ar']) > 0:
                chow = True
                issues.append('CHOW')
                placeholder = f'NEW_BELLHAVEN_CHOW_{position:03d}'
                # Do not update protected old account fields. Create a new account
                # using the website's current canonical data, then link the old one.
                changes = [proposed_creation(site, placeholder), {
                    'action': 'UPDATE_FIELD_AFTER_CREATION',
                    'account_id': account['account_id'],
                    'field': 'chow_current_account',
                    'from': account.get('chow_current_account', ''),
                    'to_placeholder': placeholder,
                    'details': 'Resolve placeholder to newly created CRM account ID before any later execution. Do NOT change the old parent_id.',
                }]
            else:
                changes.insert(0, {'action': 'UPDATE_FIELDS', 'account_id': account['account_id'],
                                   'fields': {'parent_id': {'from': account['parent_id'], 'to': PARENT_ID},
                                              'parent_name': {'from': account['parent_name'], 'to': PARENT_NAME}}})
        if account['status'] != 'Active':
            issues.append('INACTIVE_ACCOUNT')
            if not chow:
                changes.append({'action': 'UPDATE_FIELD', 'account_id': account['account_id'], 'field': 'status', 'from': account['status'], 'to': 'Active'})
        category = 'NEEDS_FIX' if issues else 'CONFIDENT_MATCH'
        proposals.append({
            'category': category, 'website_location': site, 'crm_account': account,
            'evidence': {'matched_fields': [key for key in ('name', 'city', 'state', 'zip_code', 'street_address', 'care_type', 'parent_id') if e[key].get('normalized_match', e[key].get('match', False))],
                         'compared_fields': e, 'candidate_score': round(score, 2)},
            'issues': list(dict.fromkeys(issues)), 'proposed_changes': changes if changes else [{'action': 'NO_CHANGE'}],
            'requires_review': bool(changes), 'chow': chow,
        })

    # Duplicate detection: same city/state/ZIP, normalized street and strong name,
    # never address alone (several competing operators share physical addresses).
    groups = defaultdict(list)
    for account in accounts:
        if not account['billing_street']:
            continue
        key = (normalize(account['billing_city']), normalize(account['billing_state']),
               str(account['billing_zip']), address_key(account['billing_street']))
        groups[key].append(account)
    duplicate_losers = set()
    for location_key, group in groups.items():
        for i, a in enumerate(group):
            if a['account_id'] in duplicate_losers:
                continue
            cluster = [a]
            for b in group[i+1:]:
                if b['account_id'] not in duplicate_losers and normalize(a['name']) == normalize(b['name']):
                    cluster.append(b)
            if len(cluster) < 2:
                continue
            # Survive account already selected for a website match, otherwise
            # prefer a linked Bellhaven account and an account with activity.
            cluster.sort(key=lambda x: (
                x['account_id'] in used_ids,
                x['parent_id'] == PARENT_ID,
                money(x['lifetime_revenue']) > 0 or money(x['outstanding_ar']) > 0,
                x['account_id'],
            ), reverse=True)
            survivor = cluster[0]
            for loser in cluster[1:]:
                duplicate_losers.add(loser['account_id'])
                proposals.append({
                    'category': 'DUPLICATE', 'website_location': next((s for s in sites if normalize(s['city']) == location_key[0] and str(s['zip_code']) == location_key[2] and address_key(s['street_address']) == location_key[3]), None),
                    'crm_account': loser, 'surviving_crm_account': survivor,
                    'evidence': {'same_normalized_name': normalize(loser['name']) == normalize(survivor['name']),
                                 'same_city_state_zip_and_street': True,
                                 'survivor_reason': 'Prefer website-selected account, then correct parent and account activity.'},
                    'issues': ['DUPLICATE_ACCOUNT'],
                    'proposed_changes': [
                        {'action': 'UPDATE_FIELD', 'account_id': loser['account_id'], 'field': 'duplicate_of_account', 'from': loser.get('duplicate_of_account', ''), 'to': survivor['account_id']},
                        {'action': 'UPDATE_FIELD', 'account_id': loser['account_id'], 'field': 'status', 'from': loser['status'], 'to': 'Inactive'},
                    ], 'requires_review': True, 'chow': False,
                })

    # An account that is a duplicate is handled in DUPLICATE rather than also
    # receiving an unrelated NO_LONGER_LISTED proposal.
    for account in accounts:
        if account['parent_id'] != PARENT_ID or account['account_id'] in used_ids or account['account_id'] in duplicate_losers or account['account_id'] in reviewed_accounts:
            continue
        proposals.append({
            'category': 'NO_LONGER_LISTED', 'website_location': None,
            'crm_account': account,
            'evidence': {'reason': 'CRM account belongs to Bellhaven parent but was not linked to any of the website communities.',
                         'website_communities_checked': len(sites)},
            'issues': ['NOT_ON_WEBSITE'],
            'proposed_changes': [
                {'action': 'UPDATE_FIELD', 'account_id': account['account_id'], 'field': 'status', 'from': account['status'], 'to': 'Needs Review'},
                {'action': 'UPDATE_FIELD', 'account_id': account['account_id'], 'field': 'note', 'from': account.get('note', ''), 'to': 'Needs review: Bellhaven CRM facility not found on current Bellhaven website listing; verify ownership and operating status before any further action.'},
            ], 'requires_review': True, 'chow': False,
        })

    original_total = len(proposals)
    proposals = [p for p in proposals if proposal_key(p) not in decided_keys
                 and not (p['category'] in ('DUPLICATE', 'NO_LONGER_LISTED')
                          and p['crm_account']['account_id'] in reviewed_accounts)]
    print(f'Skipped {original_total - len(proposals)} already-decided proposals')
    counts = Counter(p['category'] for p in proposals)
    payload = {
        'report_type': 'Bellhaven CRM review-only reconciliation proposals',
        'source_files': [WEBSITE_FILE.name, CRM_FILE.name],
        'bellhaven_parent_account_id': PARENT_ID,
        'website_location_count': len(sites),
        'crm_account_count': len(accounts),
        'summary': {key: counts[key] for key in ('CONFIDENT_MATCH', 'NEEDS_FIX', 'CREATE_NEW', 'DUPLICATE', 'NO_LONGER_LISTED')},
        'chow_proposals': sum(p['chow'] for p in proposals),
        'proposals': proposals,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'Wrote {OUTPUT_FILE.name} (no API calls or CRM changes)')
    print(f'Website rows: {len(sites)} | CRM accounts: {len(accounts)}')
    for category, total in payload['summary'].items():
        print(f'{category}: {total}')
    print(f'CHOW flagged (included in NEEDS_FIX): {payload["chow_proposals"]}')


if __name__ == '__main__':
    main()
