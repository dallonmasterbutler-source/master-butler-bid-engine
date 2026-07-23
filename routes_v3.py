"""
LIGHTS ROUTES v3 — LAST YEAR'S REAL ROUTES AS THE STANDARD (Dallon,
Jul 22: "use last years routes as the standard... all toms homes have
to stay on him... iron man and Monroe circuit are chosen [mine]...
Take all of sammamish and make it into 3 similar routes").

Ground truth: data/lastseason_visits.json — every lights-install visit
Sep 2025–Jan 2026 with its assigned tech, fetched read-only from
Jobber (refetch with fetch_lastseason() if stale). Rules:
  · Tom's homes locked to Tom; Dallon's Iron Man/Monroe circuit locked
    to Dallon (his choice, 6 years standing).
  · Sammamish: 3 similar routes, ZERO core churn — Connor/Gavin/
    Nicholas keep every home they had; only new/unmatched homes are
    dealt to balance the three.
  · Everyone else keeps last year's homes; new/unmatched non-Sammamish
    homes join the nearest unlocked route (reported as 'added').

Run: python3 routes_v3.py   → writes data/samm_routes.json (push the
blob + rebuild samm_sched with samm_routes.build_schedule after).
"""
import json
import re
import samm_routes as sr

FIXED = {
    'Tom Fricke':      ("Yoda — Tom's homes (untouched)", "#334155", True),
    'Dallon Anderson': ("Iron Man — Dallon's Monroe circuit (his, 6 yrs)",
                        "#b8860b", True),
    'Shane Strand':    ("Superman — Shane (last year's homes)",
                        "#1d4ed8", False),
    'Mark Proudlock':  ("Wolverine — Mark (last year's homes)",
                        "#6d28d9", False),
    'Austin Dayton':   ("Spiderman — Austin (last year's homes)",
                        "#dc2626", False),
    'Adam Mcbride':    ("Batman — Adam (last year's homes)",
                        "#0f172a", False),
    'Nick Karvonen':   ("Nick K — his pocket (small; office decides)",
                        "#0891b2", False),
}
ANCH = ['Connor Anderson', 'Gavin Ojalehto', 'Nicholas Bush']
SAMM = [("Sammamish 1 — Connor's core (Flash)", "#c0392b"),
        ("Sammamish 2 — Gavin's core (Optimus)", "#8e44ad"),
        ("Sammamish 3 — Nicholas's core (Captain America)", "#1a6b3c")]


def _akey(a):
    a = (a or '').lower()
    m = re.match(r'\s*(\d+)\s+([a-z0-9]+)', a)
    return (m.group(1) + ' ' + m.group(2)) if m else ''


def _tech_map():
    visits = json.load(open('data/lastseason_visits.json'))
    by_addr, by_client = {}, {}
    for v in sorted(visits, key=lambda v: v['start']):
        if not v['techs']:
            continue
        t = v['techs'][0].strip()
        if _akey(v['addr']):
            by_addr[_akey(v['addr'])] = t
        if sr._nkey(v['client']):
            by_client[sr._nkey(v['client'])] = t
    return by_addr, by_client


def build():
    by_addr, by_client = _tech_map()
    homes = json.load(open('data/lights_homes.json'))
    cal = json.loads(open('data/lights_calibration.json').read())
    latest = {}
    for li in cal.get('all_lines') or []:
        c = sr._nkey(li.get('client'))
        d = (li.get('date') or '')[:10]
        if c and d >= '2025-08':
            latest[c] = max(latest.get(c, ''), d)
    out_homes = []
    for h in homes:
        if not (h.get('lat') and h.get('lng')
                and 47.2 < h['lat'] < 48.2
                and -122.6 < h['lng'] < -121.4):
            continue
        h['tech_ly'] = (by_addr.get(_akey(h.get('address')))
                        or by_client.get(sr._nkey(h.get('client'))))
        h['active'] = sr._nkey(h.get('client')) in latest
        out_homes.append(h)
    homes = out_homes
    is_samm = lambda h: (h.get('city') or '').lower() == 'sammamish'

    routes = {t: {'name': n, 'color': c, 'locked': lk,
                  'core': [], 'added': []}
              for t, (n, c, lk) in FIXED.items()}
    samm = {i: {'name': SAMM[i][0], 'color': SAMM[i][1],
                'core': [], 'added': []} for i in range(3)}
    floats = []
    for h in homes:
        t = h.get('tech_ly')
        if t in ('Tom Fricke', 'Dallon Anderson'):
            routes[t]['core'].append(h)
        elif is_samm(h) and t in ANCH:
            samm[ANCH.index(t)]['core'].append(h)
        elif not is_samm(h) and t in routes:
            routes[t]['core'].append(h)
        else:
            floats.append(h)

    def cent(hs):
        return ((sum(h['lat'] for h in hs) / len(hs),
                 sum(h['lng'] for h in hs) / len(hs))
                if hs else (47.6, -122.0))
    for r in routes.values():
        r['cent'] = cent(r['core'])
    for s in samm.values():
        s['cent'] = cent(s['core'])
    for h in sorted(floats, key=lambda h: (h['lat'], h['lng'])):
        if is_samm(h):
            best = min(range(3), key=lambda i: sr._dist(
                (h['lat'], h['lng']), samm[i]['cent'])
                + 0.02 * (len(samm[i]['core']) + len(samm[i]['added'])))
            samm[best]['added'].append(h)
        else:
            cands = [r for r in routes.values() if not r['locked']]
            best = min(cands, key=lambda r: sr._dist(
                (h['lat'], h['lng']), r['cent']))
            best['added'].append(h)

    def mk(name, color, tech, core, added, locked=False):
        hs = sorted(core + added, key=lambda h: (h['lat'], h['lng']))
        c0 = cent(hs)
        return {'name': name, 'tech': tech, 'color': color,
                'count': len(hs),
                'active': sum(1 for h in hs if h['active']),
                'core': len(core),
                'added_active': sum(1 for h in added if h['active']),
                'locked': locked, 'center': c0,
                'max_mi_from_center': round(max(
                    (sr._dist((h['lat'], h['lng']), c0) for h in hs),
                    default=0), 1),
                'homes': [{'client': h['client'],
                           'address': h['address'],
                           'lat': h['lat'], 'lng': h['lng'],
                           'bulb': h.get('bulb'),
                           'active': h['active'],
                           'tech_ly': h.get('tech_ly')} for h in hs]}
    out_routes = [mk(r['name'], r['color'], t, r['core'], r['added'],
                     r['locked']) for t, r in routes.items()]
    out_routes += [mk(s['name'], s['color'], ANCH[i] + ' (anchor)',
                      s['core'], s['added'])
                   for i, s in samm.items()]
    return {'routes': out_routes, 'bad_geocodes': [],
            'total_homes': sum(r['count'] for r in out_routes),
            'total_active': sum(r['active'] for r in out_routes),
            'sammamish_active': sum(r['active'] for r in out_routes[-3:]),
            'standard': ('per-home last-year tech (Jobber visits '
                         'Sep 2025 - Jan 2026); cores never move')}


if __name__ == '__main__':
    o = build()
    json.dump(o, open('data/samm_routes.json', 'w'))
    for r in o['routes']:
        print(f"  {r['name'][:50]:52s} {r['active']:3d} active "
              f"({r['core']:3d} core + {r['added_active']:2d} added)"
              + (' 🔒' if r['locked'] else ''))
