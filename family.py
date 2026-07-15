"""Reconstruct family structure inside one household (for house_overload review).

Electoral rolls record, for each elector, a relation (father / mother /
husband / wife) and that relation's name. Within a single house we can resolve
those references to actual co-residents and rebuild the family units.

Why this matters for fraud: a genuinely large household is a few connected
families. A *stuffed* address looks different — many electors with no family
link to anyone else at the address, or links that are impossible (a "father"
younger than the child). Those are the leads this module surfaces.

Fairness note: an elector whose relation lives elsewhere (a married woman
whose father is in another village, a migrant whose family stayed home) is
normal. Being UNLINKED here is a lead only because the house is already
overloaded — it is never treated as a verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PARENT_TYPES = {"F", "M", "FTHR", "MTHR", "FATHER", "MOTHER"}
SPOUSE_TYPES = {"H", "W", "HSBN", "HUSBAND", "WIFE"}
MIN_PARENT_GAP = 13   # a parent must be at least this many years older


@dataclass
class Household:
    members: list                       # voter rows (dicts), roll order
    edges: list                         # (from_id, to_id, kind); parent edge = (child, parent)
    clusters: list                      # connected components (lists of ids), largest first
    unlinked: list                      # ids with no family link either way
    anomalies: dict = field(default_factory=dict)   # voter_id -> [notes]
    signals: list = field(default_factory=list)     # house-level findings


# ---------------------------------------------------------------- helpers
def _gender(m) -> str:
    g = str(m.get("gender") or "").strip().upper()
    return g[0] if g[:1] in ("M", "F") else "?"


def _rel_kind(m) -> str:
    t = str(m.get("relation_type") or "").strip().upper()
    if t in PARENT_TYPES:
        return "parent"
    if t in SPOUSE_TYPES:
        return "spouse"
    return ""


def _name_match(a: str, b: str) -> bool:
    """Match a recorded relation name against an elector's name.

    Exact normalised match, or token-subset ("RAM KUMAR" vs "RAM KUMAR SINGH")
    when the shorter form has >= 2 tokens. A single-token name only matches the
    other's FIRST token (given name), and only if reasonably long — this keeps
    "RAM" from linking to every "...RAM..." in the house.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(small) >= 2 and set(small) <= set(big):
        return True
    return len(small) == 1 and len(small[0]) >= 4 and small[0] == big[0]


def _link_score(m, o, kind: str) -> float:
    """Rank candidate targets for m's relation reference. Gender fit and a
    plausible age gap beat a bare name collision."""
    s = 0.0
    t = str(m.get("relation_type") or "").strip().upper()[:1]
    want = {"F": "M", "M": "F", "H": "M", "W": "F"}.get(t)
    if want and _gender(o) == want:
        s += 2
    am, ao = m.get("age"), o.get("age")
    if am and ao:
        if kind == "parent":
            gap = ao - am
            s += 2 if MIN_PARENT_GAP <= gap <= 60 else (1 if gap >= MIN_PARENT_GAP else -2)
        else:  # spouse
            s += 1 if abs(am - ao) <= 20 else -1
    if (m.get("relation_name_norm") or "") == (o.get("name_norm") or ""):
        s += 1   # exact spelling beats fuzzy subset
    return s


# ---------------------------------------------------------------- analysis
def analyse_household(members) -> Household:
    """Link every elector to the co-resident their relation names, cluster the
    result into family groups, and collect anomalies + house-level signals."""
    ms = [dict(m) for m in members]
    by_id = {m["id"]: m for m in ms}
    anomalies: dict[int, list[str]] = {}

    def note(vid: int, msg: str):
        anomalies.setdefault(vid, []).append(msg)

    # ---- resolve relation references to people inside the house
    edges: list[tuple[int, int, str]] = []
    referenced: set[int] = set()
    spouse_refs: dict[int, int] = {}       # target -> how many call them spouse
    external_spouses = 0
    for m in ms:
        rel, kind = m.get("relation_name_norm") or "", _rel_kind(m)
        if not rel or not kind:
            continue
        cands = [o for o in ms
                 if o["id"] != m["id"] and _name_match(rel, o.get("name_norm") or "")]
        if not cands:
            if kind == "spouse":
                external_spouses += 1     # spouse not enrolled here — informational
            continue
        best = max(cands, key=lambda o: _link_score(m, o, kind))
        edges.append((m["id"], best["id"], kind))
        referenced.add(best["id"])
        if kind == "spouse":
            spouse_refs[best["id"]] = spouse_refs.get(best["id"], 0) + 1

        # sanity-check the chosen link
        am, ao = m.get("age"), best.get("age")
        if kind == "parent" and am and ao and ao - am < MIN_PARENT_GAP:
            msg = (f"impossible relation: listed parent '{best['name']}' is "
                   f"{ao}, elector is {am}")
            note(m["id"], msg)
            note(best["id"], f"named as parent of {m['name']} ({am}) while only {ao}")
        t = str(m.get("relation_type") or "").strip().upper()[:1]
        want = {"F": "M", "M": "F", "H": "M", "W": "F"}.get(t)
        if want and _gender(best) not in ("?", want):
            note(m["id"], f"relation gender mismatch: '{best['name']}' recorded "
                          f"as {m.get('relation_type')} but is {_gender(best)}")

    for vid, n in spouse_refs.items():
        if n > 1:
            note(vid, f"named as spouse by {n} different electors")

    # ---- duplicate exact names inside the house
    seen: dict[str, list[int]] = {}
    for m in ms:
        if m.get("name_norm"):
            seen.setdefault(m["name_norm"], []).append(m["id"])
    dup_ids = [vid for ids in seen.values() if len(ids) > 1 for vid in ids]
    for vid in dup_ids:
        note(vid, f"name appears {len(seen[by_id[vid]['name_norm']])}× in this house")

    # ---- connected components over the family links
    parent = {m["id"]: m["id"] for m in ms}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, _ in edges:
        parent[find(a)] = find(b)

    groups: dict[int, list[int]] = {}
    for m in ms:
        groups.setdefault(find(m["id"]), []).append(m["id"])
    clusters = sorted(groups.values(), key=len, reverse=True)

    linked = {a for a, _, _ in edges} | referenced
    unlinked = [m["id"] for m in ms if m["id"] not in linked]

    # ---- house-level signals, most damning first
    n = len(ms)
    fams = [c for c in clusters if len(c) >= 2]
    signals: list[str] = []
    if unlinked:
        signals.append(f"🚩 **{len(unlinked)} of {n} electors have no family link "
                       f"to anyone in this house** — prime leads for roll stuffing.")
    impossible = sum(1 for notes in anomalies.values()
                     for x in notes if x.startswith("impossible"))
    if impossible:
        signals.append(f"🚩 **{impossible} impossible parent/child link(s)** — "
                       "ages contradict the recorded relation.")
    if dup_ids:
        signals.append(f"🟠 {len(dup_ids)} electors share an identical name "
                       "within this house (possible double entries).")
    if fams:
        sizes = ", ".join(str(len(c)) for c in fams)
        signals.append(f"{len(fams)} family group(s) reconstructed "
                       f"(sizes: {sizes}); "
                       f"{n - sum(len(c) for c in fams)} elector(s) outside any group.")
        if len(fams) >= 4:
            signals.append(f"🟠 {len(fams)} unrelated family groups at one address — "
                           "check whether this is a real multi-family building.")
    else:
        signals.append("🚩 **No family links found at all** between the electors "
                       "of this house.")
    if external_spouses:
        signals.append(f"ℹ️ {external_spouses} elector(s) name a spouse who is not "
                       "enrolled at this address (often legitimate).")

    return Household(members=ms, edges=edges, clusters=clusters,
                     unlinked=unlinked, anomalies=anomalies, signals=signals)


# ---------------------------------------------------------------- rendering
def _esc(s) -> str:
    return str(s).replace("\\", r"\\").replace('"', r"\"")


def cluster_dot(hh: Household, cluster: list[int]) -> str:
    """Graphviz DOT for one family group. Parent → child arrows, undirected
    purple edge for spouses, red fill for anomalous members."""
    by_id = {m["id"]: m for m in hh.members}
    ids = set(cluster)
    out = [
        "digraph family {",
        '  rankdir=TB; bgcolor="transparent";',
        '  node [shape=box style="rounded,filled" fillcolor="#eef2f7" '
        'color="#90a4ae" fontname="Helvetica" fontsize=11];',
    ]
    for vid in cluster:
        m = by_id[vid]
        label = (f"{_esc(m.get('name'))}\\n{_gender(m)} · "
                 f"{m.get('age') or '?'}y · S.No {m.get('serial_no') or '?'}")
        style = ' fillcolor="#fdecea" color="#c62828"' if vid in hh.anomalies else ""
        out.append(f'  v{vid} [label="{label}"{style}];')
    drawn_spouse = set()
    for a, b, kind in hh.edges:
        if a not in ids or b not in ids:
            continue
        if kind == "spouse":
            pair = frozenset((a, b))
            if pair in drawn_spouse:
                continue
            drawn_spouse.add(pair)
            out.append(f'  v{a} -> v{b} [dir=none color="#8e24aa" penwidth=2];')
        else:
            out.append(f'  v{b} -> v{a} [color="#546e7a"];')
    out.append("}")
    return "\n".join(out)
