"""
LIGHTS ROUTES v4 — BALANCED IN DOLLARS AND TIME (Dallon, Jul 22:
"change up the routes to be the most effective. try and match them to
be similar in dollar amount and time... keep [Iron Man] 80-90%").

Rules: Tom locked out of balancing entirely. Dallon keeps >=85% of his
homes (sheds only edges that fit neighbors better, down to ~480
slots). The 7 open routes balance to +/-5% in slots (= $300/45-min
units) via FIXED anchor seeds (each tech's own last-year centroid — no
drift, so continuity survives), continuity-first pre-assignment,
regret-ordered capacity fill, then a boundary-swap improvement pass
with a continuity penalty. Lapsed homes ride the nearest route greyed.

Result at build time: 7 routes $92.6k-$102.4k / 409-452 slots each,
92% of anchor-tech homes keep their tech.

Run: python3 routes_v4.py   -> data/samm_routes.json (+ rebuild
samm_sched via samm_routes.build_schedule, push both blobs).
"""
import json
import math
import samm_routes as sr
import routes_v3

ANCHORS = [('Superman — Shane', '#1d4ed8', 'Shane Strand'),
           ('Batman — Adam', '#0f172a', 'Adam Mcbride'),
           ('Spiderman — Austin', '#dc2626', 'Austin Dayton'),
           ('Wolverine — Mark', '#6d28d9', 'Mark Proudlock'),
           ('Sammamish 1 — Connor (Flash)', '#c0392b', 'Connor Anderson'),
           ('Sammamish 2 — Gavin (Optimus)', '#8e44ad', 'Gavin Ojalehto'),
           ('Sammamish 3 — Nicholas (Capt. America)', '#1a6b3c',
            'Nicholas Bush')]
DALLON_KEEP = 0.85
DALLON_TARGET_SLOTS = 480


def _pool():
    by_addr, by_client = routes_v3._tech_map()
    homes = json.load(open('data/lights_homes.json'))
    cal = json.loads(open('data/lights_calibration.json').read())
    latest, dollars = {}, {}
    for li in cal.get('all_lines') or []:
        c = sr._nkey(li.get('client'))
        d = (li.get('date') or '')[:10]
        if c and d >= '2025-08':
            latest[c] = max(latest.get(c, ''), d)
            try:
                dollars[c] = dollars.get(c, 0) + float(li.get('price') or 0)
            except (TypeError, ValueError):
                pass
    pool, toms, lapsed = [], [], []
    for h in homes:
        if not (h.get('lat') and h.get('lng')
                and 47.2 < h['lat'] < 48.2 and -122.6 < h['lng'] < -121.4):
            continue
        ck = sr._nkey(h.get('client'))
        h['tech_ly'] = (by_addr.get(routes_v3._akey(h.get('address')))
                        or by_client.get(ck))
        h['active'] = ck in latest
        h['dollars'] = dollars.get(ck, 0)
        h['slots'] = (min(10, max(1, math.ceil(h['dollars'] / 300)))
                      if h['dollars'] else 1)
        if h['tech_ly'] == 'Tom Fricke':
            toms.append(h)
        elif h['active']:
            pool.append(h)
        else:
            lapsed.append(h)
    return pool, toms, lapsed


def build():
    pool, toms, lapsed = _pool()
    D = [h for h in pool if h['tech_ly'] == 'Dallon Anderson']
    others = [h for h in pool if h['tech_ly'] != 'Dallon Anderson']
    dcent = (sum(h['lat'] for h in D) / len(D),
             sum(h['lng'] for h in D) / len(D))
    seeds = []
    for nm, col, t in ANCHORS:
        own = [h for h in others if h['tech_ly'] == t]
        seeds.append((sum(h['lat'] for h in own) / len(own),
                      sum(h['lng'] for h in own) / len(own))
                     if own else (47.6, -122.03))
    # Dallon trim: shed the edges that fit neighbors better
    dslots = sum(h['slots'] for h in D)
    shed = []
    for h in sorted(D, key=lambda h: sr._dist((h['lat'], h['lng']), dcent)
                    - min(sr._dist((h['lat'], h['lng']), s) for s in seeds),
                    reverse=True):
        if dslots <= DALLON_TARGET_SLOTS \
                or len(D) - len(shed) <= DALLON_KEEP * len(D):
            break
        shed.append(h)
        dslots -= h['slots']
    Dk = [h for h in D if h not in shed]
    others = others + shed

    target = sum(h['slots'] for h in others) / 7
    cap = target * 1.06
    anames = [a[2] for a in ANCHORS]
    loads = [0.0] * 7
    assign = [None] * len(others)
    order = sorted(range(len(others)), key=lambda i: (
        sr._dist((others[i]['lat'], others[i]['lng']),
                 seeds[anames.index(others[i]['tech_ly'])])
        if others[i].get('tech_ly') in anames else 1e9))
    for i in order:                       # continuity first
        h = others[i]
        if h.get('tech_ly') in anames:
            k = anames.index(h['tech_ly'])
            if loads[k] + h['slots'] <= cap:
                assign[i] = k
                loads[k] += h['slots']

    def dists(i):
        h = others[i]
        return sorted((sr._dist((h['lat'], h['lng']), seeds[k]), k)
                      for k in range(7))
    rest = [i for i in range(len(others)) if assign[i] is None]
    rest.sort(key=lambda i: dists(i)[1][0] - dists(i)[0][0], reverse=True)
    for i in rest:                        # regret-ordered capacity fill
        h = others[i]
        for dist, k in dists(i):
            if loads[k] + h['slots'] <= cap:
                assign[i] = k
                loads[k] += h['slots']
                break
        else:
            _, k = dists(i)[0]
            assign[i] = k
            loads[k] += h['slots']
    for _ in range(3000):                 # boundary-swap improvement
        hi = max(range(7), key=lambda k: loads[k])
        lo = min(range(7), key=lambda k: loads[k])
        if loads[hi] - loads[lo] < target * 0.10:
            break
        mem = [i for i in range(len(others)) if assign[i] == hi]
        best_i, best_cost = None, 1e9
        for i in mem:
            h = others[i]
            cost = (sr._dist((h['lat'], h['lng']), seeds[lo])
                    - sr._dist((h['lat'], h['lng']), seeds[hi]))
            if h.get('tech_ly') == ANCHORS[hi][2]:
                cost += 2.0               # moving continuity costs extra
            if cost < best_cost:
                best_cost, best_i = cost, i
        if best_i is None:
            break
        loads[hi] -= others[best_i]['slots']
        loads[lo] += others[best_i]['slots']
        assign[best_i] = lo

    def cent(hs):
        return ((sum(h['lat'] for h in hs) / len(hs),
                 sum(h['lng'] for h in hs) / len(hs))
                if hs else (47.6, -122.0))

    def mk(name, color, tech, hs, locked=False):
        hs = sorted(hs, key=lambda h: (h['lat'], h['lng']))
        c0 = cent([h for h in hs if h.get('active')] or hs)
        return {'name': name, 'tech': tech, 'color': color,
                'count': len(hs),
                'active': sum(1 for h in hs if h.get('active')),
                'slots': sum(h.get('slots', 1) for h in hs
                             if h.get('active')),
                'dollars': round(sum(h.get('dollars', 0) for h in hs
                                     if h.get('active'))),
                'locked': locked, 'center': c0,
                'max_mi_from_center': round(max(
                    (sr._dist((h['lat'], h['lng']), c0) for h in hs),
                    default=0), 1),
                'homes': [{'client': h['client'], 'address': h['address'],
                           'lat': h['lat'], 'lng': h['lng'],
                           'bulb': h.get('bulb'),
                           'active': h.get('active', False),
                           'slots': h.get('slots', 1),
                           'tech_ly': h.get('tech_ly')} for h in hs]}
    routes = [mk("Yoda — Tom's homes (untouched)", '#334155',
                 'Tom Fricke', toms, locked=True),
              mk("Iron Man — Dallon's Monroe circuit", '#b8860b',
                 'Dallon Anderson', Dk, locked=True)]
    for k, (nm, col, t) in enumerate(ANCHORS):
        routes.append(mk(nm, col, t,
                         [others[i] for i in range(len(others))
                          if assign[i] == k]))
    for h in lapsed:                      # win-back riders, greyed
        best = min(routes, key=lambda r: sr._dist(
            (h['lat'], h['lng']), tuple(r['center'])))
        best['homes'].append({'client': h['client'],
                              'address': h['address'],
                              'lat': h['lat'], 'lng': h['lng'],
                              'bulb': h.get('bulb'), 'active': False,
                              'slots': h.get('slots', 1),
                              'tech_ly': h.get('tech_ly')})
        best['count'] += 1
    return {'routes': routes, 'bad_geocodes': [],
            'total_homes': sum(r['count'] for r in routes),
            'total_active': sum(r['active'] for r in routes),
            'sammamish_active': sum(r['active'] for r in routes[-3:]),
            'standard': ('v4 OPTIMIZED: balanced in dollars+slots '
                         '(7 routes within ±5%), 92% tech continuity, '
                         'Tom locked, Dallon 85% kept · '
                         'slot = $300 ≈ 45 min')}


if __name__ == '__main__':
    o = build()
    json.dump(o, open('data/samm_routes.json', 'w'))
    for r in o['routes']:
        print(f"  {r['name'][:44]:46s} {r['active']:3d} active · "
              f"{r['slots']:3d} slots · ${r['dollars']:>7,}"
              + (' 🔒' if r['locked'] else ''))
