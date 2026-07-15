"""Shared Streamlit pieces for the review pages: flag cards, the grouped
house_overload view with family-tree reconstruction, and infinite scroll."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from family import analyse_household, cluster_dot
from fraud_rules import get_photo, house_members

# ---------------------------------------------------------------- infinite scroll
PAGE_STEP = 100

_autoload = components.declare_component(
    "autoload", path=str(Path(__file__).parent / "components" / "autoload"))


def infinite_limit(state_key: str) -> int:
    """How many rows to fetch right now for this list."""
    return PAGE_STEP * (1 + st.session_state.get(state_key, 0))


def infinite_scroll_sentinel(state_key: str, has_more: bool) -> None:
    """Place at the very bottom of the list. Scrolling it into view bumps the
    page counter and reruns, which loads PAGE_STEP more rows."""
    fire = _autoload(has_more=has_more, key=f"sentinel::{state_key}", default=0)
    if has_more:
        st.caption("Loading more as you scroll…")
        # The component sends a fresh timestamp each time the sentinel is
        # visible; any value we haven't seen yet means "load another page".
        seen_key = f"{state_key}::seen"
        if fire and fire != st.session_state.get(seen_key):
            st.session_state[seen_key] = fire
            st.session_state[state_key] = st.session_state.get(state_key, 0) + 1
            st.rerun()
    else:
        st.caption("— end of list —")


# ---------------------------------------------------------------- flag cards
def flag_title(f) -> str:
    sev_icon = {"high": "🔴", "medium": "🟠"}.get(f["severity"], "🟡")
    d = f.get("details") or {}
    if f["rule"] == "house_overload" and d.get("house_norm"):
        return (f"{sev_icon} **house_overload** — House {d.get('house') or '?'} "
                f"(AC {d.get('constituency_no') or '?'}) — "
                f"{d.get('occupants', '?')} electors")
    return (f"{sev_icon} **{f['rule']}** — {f['name_a']} "
            f"({f['epic_a'] or 'no EPIC'})"
            + (f"  ↔  {f['name_b']} ({f['epic_b'] or 'no EPIC'})"
               if f["name_b"] else ""))


def _voter_md(f, side: str) -> str:
    return (f"**{f['name_' + side]}**  \n"
            f"EPIC: `{f['epic_' + side]}`  \n"
            f"AC {f['const_' + side] or '?'} · Part {f['part_' + side]} · "
            f"Serial {f['serial_' + side] if f['serial_' + side] is not None else '?'}  \n"
            f"House {f['house_' + side]}  \n"
            f"Age {f['age_' + side]} · {f['gender_' + side]}")


def flag_card(f) -> None:
    """Body of one flag expander: pair of voter cards, or — for a grouped
    house_overload flag — every occupant plus the reconstructed family tree."""
    d = f.get("details") or {}
    if f["rule"] == "house_overload" and d.get("house_norm"):
        _house_overload_card(f, d)
        return

    cols = st.columns([2, 1, 2, 1]) if f["name_b"] else st.columns([2, 1])
    cols[0].markdown(_voter_md(f, "a"))
    pa = get_photo(f["voter_id"])
    if pa:
        cols[1].image(pa, width=110)
    if f["name_b"]:
        cols[2].markdown(_voter_md(f, "b"))
        pb = get_photo(f["related_voter_id"])
        if pb:
            cols[3].image(pb, width=110)
    st.json(f["details"], expanded=False)


def _house_overload_card(f, d: dict) -> None:
    members = house_members(d.get("constituency_no"), d["house_norm"])
    if not members:
        st.warning("No electors found for this house any more (data re-ingested?).")
        st.json(d, expanded=False)
        return

    hh = analyse_household(members)
    by_id = {m["id"]: m for m in hh.members}

    st.markdown(f"### 🏠 House `{d.get('house') or f['house_a']}` — "
                f"AC {d.get('constituency_no') or '?'} — "
                f"**{len(members)} electors** at this address")
    for line in hh.signals:
        st.markdown(f"- {line}")

    # ---- reconstructed family groups (tree per group)
    fams = [c for c in hh.clusters if len(c) >= 2]
    if fams:
        st.markdown("**Family groups** (arrows: parent → child, "
                    "purple line: spouses, red: anomaly):")
        for i, cluster in enumerate(fams, 1):
            with st.expander(f"Family group {i} — {len(cluster)} members",
                             expanded=len(fams) <= 3):
                st.graphviz_chart(cluster_dot(hh, cluster))

    # ---- the prime suspects: nobody in the house is family to them
    if hh.unlinked:
        st.markdown("**⚠️ Unattached electors** — no family link to anyone "
                    "here (verify these first):")
        st.dataframe(_members_df([by_id[v] for v in hh.unlinked], hh),
                     use_container_width=True, hide_index=True)

    # ---- everyone, grouped, in one table
    with st.expander(f"All {len(members)} electors in this house", expanded=False):
        group_of = {vid: i for i, c in enumerate(hh.clusters, 1)
                    for vid in c if len(c) >= 2}
        df = _members_df(hh.members, hh)
        df.insert(0, "Family", [group_of.get(m["id"], "—") for m in hh.members])
        st.dataframe(df, use_container_width=True, hide_index=True)


def _members_df(members: list, hh) -> "pd.DataFrame":
    return pd.DataFrame([{
        "Serial": m.get("serial_no"),
        "Part": m.get("part_no"),
        "Name": m.get("name"),
        "Age": m.get("age"),
        "G": m.get("gender"),
        "Relation": f"{m.get('relation_type') or ''} {m.get('relation_name') or ''}".strip(),
        "EPIC": m.get("epic_no"),
        "Notes": "; ".join(hh.anomalies.get(m["id"], [])),
    } for m in members])
