"""
LIGHTS ROUTES v6 — SENIORITY + JESSICA'S SAMMAMISH REDO (Dallon, Jul 22:
"did you remake the sammamish jobs?... jessica said she wanted to redo
the sammamish routes because they are all over. so combining them to
the best routing possible there").

v5's rule (everyone keeps 100% of last year) quietly un-did Jessica's
redo INSIDE Sammamish — the two rules only conflict there, and there
her rule wins. v6:
  · Outside Sammamish: v5 — every tech keeps 100% of last year's homes;
    seniors (Tom, Dallon, Connor, Shane, Austin) fully protected; the
    small flex pool fills the lightest routes.
  · Inside Sammamish (minus Tom's and Dallon's homes, which stay
    theirs): THREE CLEAN TERRITORIES for Connor / Gavin / Nicholas —
    fixed seeds on each tech's own center, capacity-balanced (±5%),
    with an own-home preference tuned to the knee (bonus=2): Connor
    keeps 91%, Gavin 97%, Nicholas 97%, and the ~105 stray Sammamish
    homes of OTHER techs consolidate into the three territories. The
    coherence cost vs pure geography is 0.18 mi average — the 'all
    over' problem was the strays, not the cores.
Run: python3 routes_v6.py  -> data/samm_routes.json
"""
import json
import samm_routes as sr
import routes_v4

SENIOR_LOCK = ('Tom Fricke', 'Dallon Anderson')
ANCH = ['Connor Anderson', 'Gavin Ojalehto', 'Nicholas Bush']
BONUS = 2.0
TECHS = [
    ('Tom Fricke',      "Yoda — Tom (untouched)",       '#334155', True),
    ('Dallon Anderson', "Iron Man — Dallon's circuit",  '#b8860b', True),
    ('Connor Anderson', "Flash — Connor (Sammamish E)", '#c0392b', False),
    ('Shane Strand',    "Superman — Shane's circuit",   '#1d4ed8', False),
    ('Austin Dayton',   "Spiderman — Austin's circuit", '#dc2626', False),
    ('Mark Proudlock',  "Wolverine — Mark",             '#6d28d9', False),
    ('Adam Mcbride',    "Batman — Adam",                '#0f172a', False),
    ('Gavin Ojalehto',  "Optimus — Gavin (Sammamish S)", '#8e44ad', False),
    ('Nicholas Bush',   "Captain America — Nicholas (Sammamish NW)",
     '#1a6b3c', False),
]
FLEX_OK = ('Austin Dayton', 'Mark Proudlock', 'Adam Mcbride',
           'Nicholas Bush')


def build():
    pool, toms, lapsed = routes_v4._pool()
    is_samm = lambda h: (h.get('city') or '').lower() == 'sammamish'
    names = [t[0] for t in TECHS]
    core = {t: [] for t in names}
    core['Tom Fricke'] = toms
    sp, flex = [], []
    for h in pool:
        t = h.get('tech_ly')
        if t in SENIOR_LOCK:
            core[t].append(h)
        elif is_samm(h):
            sp.append(h)                 # Sammamish redo pool
        elif t in core:
            core[t].append(h)
        else:
            flex.append(h)

    # ── Jessica's redo: 3 territories, fixed seeds, capacity, bonus
    seeds = []
    for t in ANCH:
        own = [h for h in sp if h.get('tech_ly') == t]
        seeds.append((sum(h['lat'] for h in own) / len(own),
                      sum(h['lng'] for h in own) / len(own))
                     if own else (47.61, -122.03))
    target = sum(h['slots'] for h in sp) / 3
    cap = target * 1.05
    loads = [0.0] * 3
    assign = [None] * len(sp)

    def cost(i, k):
        h = sp[i]
        c = sr._dist((h['lat'], h['lng']), seeds[k])
        if h.get('tech_ly') == ANCH[k]:
            c -= BONUS
        return c
    order = sorted(range(len(sp)), key=lambda i:
                   sorted(cost(i, k) for k in range(3))[1]
                   - sorted(cost(i, k) for k in range(3))[0],
                   reverse=True)
    for i in order:
        for k in sorted(range(3), key=lambda k: cost(i, k)):
            if loads[k] + sp[i]['slots'] <= cap:
                assign[i] = k
                loads[k] += sp[i]['slots']
                break
        else:
            k = min(range(3), key=lambda k: cost(i, k))
            assign[i] = k
            loads[k] += sp[i]['slots']
    for i, h in enumerate(sp):
        core[ANCH[assign[i]]].append(h)

    # ── flex fills the lightest non-Sammamish routes (v5 rule)
    def cent(hs):
        return ((sum(h['lat'] for h in hs) / len(hs),
                 sum(h['lng'] for h in hs) / len(hs))
                if hs else (47.6, -122.0))
    cents = {t: cent([h for h in core[t] if h.get('active')] or core[t])
             for t in names}
    fl = {t: sum(h['slots'] for h in core[t] if h.get('active'))
          for t in names}
    avg = sum(fl[t] for t in FLEX_OK) / len(FLEX_OK)
    for h in sorted(flex, key=lambda h: (h['lat'], h['lng'])):
        best = min(FLEX_OK, key=lambda t: sr._dist(
            (h['lat'], h['lng']), cents[t])
            * (1 + 1.2 * max(0, fl[t] / avg - 1)))
        core[best].append(h)
        fl[best] += h['slots']

    def mk(tech, name, color, locked):
        hs = sorted(core[tech], key=lambda h: (h['lat'], h['lng']))
        c0 = cent([h for h in hs if h.get('active')] or hs)
        kept = sum(1 for h in hs if h.get('active')
                   and h.get('tech_ly') == tech)
        had = sum(1 for h in (pool + toms)
                  if h.get('active') and h.get('tech_ly') == tech)
        return {'name': name, 'tech': tech, 'color': color,
                'count': len(hs),
                'active': sum(1 for h in hs if h.get('active')),
                'slots': sum(h.get('slots', 1) for h in hs
                             if h.get('active')),
                'dollars': round(sum(h.get('dollars', 0) for h in hs
                                     if h.get('active'))),
                'kept_pct': round(100 * kept / had) if had else 100,
                'locked': locked, 'center': c0,
                'max_mi_from_center': round(max(
                    (sr._dist((h['lat'], h['lng']), c0) for h in hs),
                    default=0), 1),
                'homes': [{'client': h['client'],
                           'address': h['address'],
                           'lat': h['lat'], 'lng': h['lng'],
                           'bulb': h.get('bulb'),
                           'active': h.get('active', False),
                           'slots': h.get('slots', 1),
                           'tech_ly': h.get('tech_ly')} for h in hs]}
    routes = [mk(t, n, c, lk) for t, n, c, lk in TECHS]
    for h in lapsed:
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
            'sammamish_active': sum(1 for r in routes
                                    for h in r['homes']
                                    if h.get('active')
                                    and 'sammamish'
                                    in (h.get('address') or '').lower()),
            'standard': ('v6: seniority holds outside Sammamish; '
                         "Jessica's Sammamish redo inside — 3 clean "
                         'territories (Connor 91% / Gavin 97% / '
                         'Nicholas 97% kept), strays consolidated · '
                         'slot = $300 ≈ 45 min')}


if __name__ == '__main__':
    o = build()
    json.dump(o, open('data/samm_routes.json', 'w'))
    for r in o['routes']:
        print(f"  {r['name'][:42]:44s} {r['active']:3d} active · "
              f"{r['slots']:3d} slots · ${r['dollars']:>7,} · "
              f"kept {r['kept_pct']}%"
              + (' 🔒' if r['locked'] else ''))
