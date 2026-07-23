"""
LIGHTS ROUTES v5 — SENIORITY HOLDS ITS ROUTE (Dallon, Jul 22: "prioritize
the numbers based on seniority. So tom, me, connor, shane, austin...
80-90% the same route, prioritize those in seniority without breaking
them based on the amount").

Rules:
  · Seniors keep 100% of their last-year homes (beats the 80-90% floor):
    Tom (locked, out of everything), Dallon, Connor, Shane, Austin.
  · Juniors (Mark, Adam, Gavin, Nicholas) keep their cores too — nobody
    is broken up — and the FLEX pool (new/unmatched homes + Nick K's
    pocket, ~53 homes) fills the lightest routes nearest-first.
  · Balance is NOT forced across seniors: a senior season may be
    Oct-heavy/Nov-light (Connor) — the schedule shows the shape instead
    of flattening it.
Run: python3 routes_v5.py  -> data/samm_routes.json
"""
import json
import samm_routes as sr
import routes_v4

TECHS = [   # seniority order; locked = out of balancing entirely
    ('Tom Fricke',      "Yoda — Tom (untouched)",            '#334155', True,  False),
    ('Dallon Anderson', "Iron Man — Dallon's circuit",       '#b8860b', True,  False),
    ('Connor Anderson', "Flash — Connor's circuit",          '#c0392b', False, False),
    ('Shane Strand',    "Superman — Shane's circuit",        '#1d4ed8', False, False),
    ('Austin Dayton',   "Spiderman — Austin's circuit",      '#dc2626', False, True),
    ('Mark Proudlock',  "Wolverine — Mark",                  '#6d28d9', False, True),
    ('Adam Mcbride',    "Batman — Adam",                     '#0f172a', False, True),
    ('Gavin Ojalehto',  "Optimus — Gavin",                   '#8e44ad', False, True),
    ('Nicholas Bush',   "Captain America — Nicholas",        '#1a6b3c', False, True),
]


def build():
    pool, toms, lapsed = routes_v4._pool()
    names = [t[0] for t in TECHS]
    core = {t: [] for t in names}
    core['Tom Fricke'] = toms
    flex = []
    for h in pool:
        t = h.get('tech_ly')
        if t in core and t != 'Tom Fricke':
            core[t].append(h)
        else:
            flex.append(h)

    def cent(hs):
        return ((sum(h['lat'] for h in hs) / len(hs),
                 sum(h['lng'] for h in hs) / len(hs))
                if hs else (47.6, -122.0))
    cents = {t: cent([h for h in core[t] if h.get('active')] or core[t])
             for t in names}
    # flex fills the LIGHT routes (flex_ok) nearest-first with a strong
    # load penalty, so geography wins locally and light routes win overall
    flex_ok = [t for t, *_ , fo in [(t[0], t[1], t[2], t[3], t[4])
               for t in TECHS] if fo]
    loads = {t: sum(h['slots'] for h in core[t] if h.get('active'))
             for t in names}
    avg = sum(loads[t] for t in flex_ok) / len(flex_ok)
    added = {t: [] for t in names}
    for h in sorted(flex, key=lambda h: (h['lat'], h['lng'])):
        best = min(flex_ok, key=lambda t: sr._dist(
            (h['lat'], h['lng']), cents[t])
            * (1 + 1.2 * max(0, loads[t] / avg - 1)))
        added[best].append(h)
        loads[best] += h['slots']

    def mk(tech, name, color, locked):
        hs = sorted(core[tech] + added[tech],
                    key=lambda h: (h['lat'], h['lng']))
        c0 = cent([h for h in hs if h.get('active')] or hs)
        return {'name': name, 'tech': tech, 'color': color,
                'count': len(hs),
                'active': sum(1 for h in hs if h.get('active')),
                'slots': sum(h.get('slots', 1) for h in hs
                             if h.get('active')),
                'dollars': round(sum(h.get('dollars', 0) for h in hs
                                     if h.get('active'))),
                'kept_pct': 100 if not added[tech] else round(
                    100 * len(core[tech]) / (len(core[tech])
                                             + len(added[tech]))),
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
    routes = [mk(t, n, c, lk) for t, n, c, lk, fo in TECHS]
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
            'sammamish_active': sum(
                1 for r in routes for h in r['homes']
                if h.get('active')
                and 'sammamish' in (h.get('address') or '').lower()),
            'standard': ('v5 SENIORITY: Tom/Dallon/Connor/Shane/Austin '
                         'keep 100% of last year; juniors keep cores; '
                         'flex (~53 new homes) fills the light routes · '
                         'slot = $300 ≈ 45 min')}


if __name__ == '__main__':
    o = build()
    json.dump(o, open('data/samm_routes.json', 'w'))
    for r in o['routes']:
        print(f"  {r['name'][:34]:36s} {r['active']:3d} active · "
              f"{r['slots']:3d} slots · ${r['dollars']:>7,} · "
              f"{r['kept_pct']}% last-yr"
              + (' 🔒' if r['locked'] else ''))
