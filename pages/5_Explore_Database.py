"""Explore the voter database: search, filter every field, browse photos."""
from __future__ import annotations

import io
import math

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import explore
from auth import require_auth
from dbx import available_years, db_ready, init_schema
from explore import Filters
from fraud_rules import get_photos

load_dotenv()

st.set_page_config(page_title="Explore Database", page_icon="🔎", layout="wide")

require_auth()
st.title("🔎 Explore the Voter Database")

ok, msg = db_ready()
if not ok:
    st.error(f"Database not reachable: {msg}")
    st.stop()
init_schema()

PAGE_SIZE = explore.PAGE_SIZE

# ===================================================================== filters
years = available_years()
year_labels = ["All years"] + [str(y) for y in years]

with st.sidebar:
    st.header("Filters")
    year_sel = st.selectbox("Revision year", year_labels, index=1 if years else 0)
    year = None if year_sel == "All years" else int(year_sel)

    opts = explore.filter_options(year)

    acs = st.multiselect("Constituency", opts["acs"])
    parts = st.multiselect("Part number", explore.parts_for(year, acs),
                           help="Choose constituencies first to narrow the list.")
    genders = st.multiselect("Gender", opts["genders"])
    relation_types = st.multiselect("Relation type", opts["relation_types"])
    statuses = st.multiselect("Enrichment status", explore.STATUS_CHOICES,
                              help="'Pending' = never looked up on ECINET.")
    category_types = st.multiselect("Category (enriched)", opts["category_types"])

    lo, hi = int(opts["age_min"]), int(opts["age_max"])
    if lo >= hi:
        hi = lo + 1
    age_min, age_max = st.slider("Age range", lo, hi, (lo, hi))
    age_flt = (age_min != lo) or (age_max != hi)

    c1, c2 = st.columns(2)
    has_mobile = c1.toggle("Has mobile", value=False,
                           help="Only voters whose ECINET record has a mobile "
                                "number.")
    has_photo = c2.toggle("Has photo", value=False)

    if st.button("Reset filters", use_container_width=True):
        for k in list(st.session_state.keys()):
            if k.startswith("exp_"):
                del st.session_state[k]
        st.rerun()

# ---------------------------------------------------------------- search box
query = st.text_input(
    "Search", key="exp_query",
    placeholder="Name, EPIC, relation, house number, or mobile — e.g. "
                "'wangsu', 'BPR0299776', '7085…'")

flt = Filters(
    year=year, acs=acs, parts=parts, genders=genders,
    relation_types=relation_types, category_types=category_types,
    statuses=statuses, has_mobile=has_mobile, has_photo=has_photo,
    query=query,
    age_min=age_min if age_flt else None,
    age_max=age_max if age_flt else None,
)

# A signature of everything that changes the result set. When it changes we
# jump back to page 1 and recompute the (expensive) total once.
sig = str((year, tuple(acs), tuple(parts), tuple(genders), tuple(relation_types),
           tuple(category_types), tuple(statuses), has_mobile, has_photo, query,
           flt.age_min, flt.age_max))
st.session_state.setdefault("exp_page", 1)
if st.session_state.get("exp_sig") != sig:
    st.session_state["exp_sig"] = sig
    st.session_state["exp_page"] = 1
    st.session_state["exp_total"] = explore.count(flt)

total = st.session_state["exp_total"]
pages = max(1, math.ceil(total / PAGE_SIZE))
# Clamp before any page widget is instantiated (safe to assign here).
st.session_state["exp_page"] = max(1, min(st.session_state["exp_page"], pages))

# ---------------------------------------------------------------- toolbar
tb = st.columns([2, 3, 3, 2])
tb[0].metric("Matches", f"{total:,}")
sort = tb[1].selectbox("Sort by", list(explore.SORTS), key="exp_sort")
view = tb[2].radio("View", ["Table", "Gallery"], horizontal=True, key="exp_view")

if total == 0:
    st.info("No voters match these filters. Try widening them or clearing the "
            "search box.")
    st.stop()

# ---------------------------------------------------------------- pagination
# Page moves go through callbacks so button clicks and the jump box share one
# source of truth (st.session_state["exp_page"]). Mutating widget-backed state
# inside a callback is the supported pattern and avoids the number_input
# snapping back over a button press.
def _go(delta_or_target, *, absolute=False) -> None:
    cur = st.session_state["exp_page"]
    nxt = delta_or_target if absolute else cur + delta_or_target
    st.session_state["exp_page"] = max(1, min(int(nxt), pages))
    st.session_state.pop("exp_table", None)  # a paged-away row selection is stale

def _jump_cb() -> None:
    st.session_state["exp_page"] = max(1, min(int(st.session_state["exp_jump"]),
                                              pages))
    st.session_state.pop("exp_table", None)

def _pager(suffix: str, *, with_jump: bool) -> None:
    p = st.session_state["exp_page"]
    cols = st.columns([1, 1, 3, 1, 1])
    cols[0].button("⏮ First", disabled=p <= 1, key=f"first_{suffix}",
                   use_container_width=True, on_click=_go, args=(1,),
                   kwargs={"absolute": True})
    cols[1].button("◀ Prev", disabled=p <= 1, key=f"prev_{suffix}",
                   use_container_width=True, on_click=_go, args=(-1,))
    if with_jump:
        # The number_input owns "exp_jump"; its callback pushes into exp_page.
        st.session_state["exp_jump"] = p
        cols[2].number_input(
            f"Page (1–{pages})", min_value=1, max_value=pages, step=1,
            key="exp_jump", on_change=_jump_cb, label_visibility="collapsed")
    else:
        cols[2].markdown(f"<div style='text-align:center;padding-top:6px'>"
                         f"page <b>{p}</b> / {pages}</div>",
                         unsafe_allow_html=True)
    cols[3].button("Next ▶", disabled=p >= pages, key=f"next_{suffix}",
                   use_container_width=True, on_click=_go, args=(1,))
    cols[4].button("Last ⏭", disabled=p >= pages, key=f"last_{suffix}",
                   use_container_width=True, on_click=_go, args=(pages,),
                   kwargs={"absolute": True})

page = st.session_state["exp_page"]
start = (page - 1) * PAGE_SIZE + 1
end = min(page * PAGE_SIZE, total)
rows = explore.page_rows(flt, sort=sort, page=page)


# ---- selection helpers + the full per-person profile ---------------------
def _pick_epic(epic: str) -> None:
    st.session_state["exp_epic"] = epic

def _clear_profile() -> None:
    st.session_state.pop("exp_epic", None)
    st.session_state.pop("exp_table", None)


def render_profile(epic: str) -> None:
    """Everything the database holds about one EPIC — every year-instance, every
    stored image, and every fraud flag that references this person."""
    prof = explore.person_profile(epic)
    prows = prof["rows"]
    if not prows:
        st.warning(f"No voter row found for EPIC {epic}.")
        return
    head = prows[0]
    status = head["epic_lookup_status"] or "Pending"
    badge = {"Found": "✅ enriched", "Not found": "❌ not found on ECINET"}.get(
        status, "🕓 not enriched yet")

    h1, h2 = st.columns([5, 1])
    h1.markdown(f"### 👤 {head['name']}\n`{epic}` · {badge} · "
                f"appears in **{len(prows)}** revision year(s)")
    h2.button("✖ Close", on_click=_clear_profile, use_container_width=True,
              key="close_profile")

    # ---- all images: one roll photo per year-instance + ECINET documents ---
    imgs = get_photos(prof["voter_ids"])
    tiles = [("roll", r) for r in prows] + [("doc", d) for d in prof["documents"]]
    st.markdown("**Photos & documents**")
    if tiles:
        cols = st.columns(min(len(tiles), 5))
        for i, (kind, obj) in enumerate(tiles):
            with cols[i % len(cols)]:
                if kind == "roll":
                    im = imgs.get(obj["id"])
                    cap = f"Roll photo · {obj['year']}"
                else:
                    im = obj["image"]
                    cap = f"{obj['doc_type']} · {obj['ext']}"
                if im:
                    st.image(im, caption=cap, use_container_width=True)
                else:
                    st.caption(f"_{cap}: none_")
    else:
        st.caption("No images stored for this person.")

    # ---- roll records, one row per revision year --------------------------
    st.markdown("**Roll records**")
    roll_cols = ["year", "constituency_no", "constituency_name", "part_no",
                 "serial_no", "epic_no", "name", "relation_type",
                 "relation_name", "house_number", "age", "gender"]
    st.dataframe(pd.DataFrame([{k: r[k] for k in roll_cols} for r in prows]),
                 hide_index=True, use_container_width=True)

    # ---- ECINET enrichment (identical across years; show once) ------------
    enriched = next((r for r in prows if r["epic_lookup_status"] == "Found"),
                    None)
    if enriched:
        enr_cols = ["verified_name", "verified_dob", "verified_age", "mobile_no",
                    "father_or_guardian_name", "mother_name", "spouse_name",
                    "verified_house_no", "verified_part_no", "part_serial_no",
                    "part_name", "ac_name", "category_type", "relation_type_code",
                    "relation_epic", "relation_name_verified", "district_cd",
                    "state_cd", "survey_channel", "submitted_for_recommendation",
                    "enum_created_on", "enum_modified_on", "lookup_officer",
                    "lookup_ac_no", "epic_id", "aadhaar_ref_no", "epic_lookup_at"]
        enr = {k: enriched[k] for k in enr_cols if enriched[k] not in (None, "")}
        st.markdown("**ECINET enrichment**")
        st.dataframe(pd.DataFrame(enr.items(), columns=["Field", "Value"]),
                     hide_index=True, use_container_width=True)
    else:
        st.info("Not enriched yet — run **EPIC Enrichment** to fetch the "
                "verified details and document images for this EPIC.")

    # ---- fraud flags on either side of a pair -----------------------------
    flags = prof["flags"]
    st.markdown(f"**Fraud flags — {len(flags)}**")
    if not flags:
        st.success("No fraud flags reference this person.")
        return
    sev_ct = {s: sum(1 for f in flags if f["severity"] == s)
              for s in ("high", "medium", "low")}
    reviewed = sum(1 for f in flags if f["verdict"])
    st.caption(f"🔴 {sev_ct['high']} high · 🟠 {sev_ct['medium']} medium · "
               f"🟡 {sev_ct['low']} low · {reviewed} reviewed. A flag is a lead, "
               "not a verdict.")

    def _other(f):
        a_is_this = f["epic_a"] == epic
        n = f["name_b"] if a_is_this else f["name_a"]
        e = f["epic_b"] if a_is_this else f["epic_a"]
        yr = f["year_b"] if a_is_this else f["year_a"]
        ac = f["const_b"] if a_is_this else f["const_a"]
        return n, e, yr, ac

    fdf = []
    for f in flags:
        n, e, yr, ac = _other(f)
        fdf.append({
            "severity": f["severity"], "rule": f["rule"],
            "score": round(f["score"], 3) if isinstance(f["score"], float)
            else f["score"],
            "matches": n or "—", "their EPIC": e or "—",
            "their year": yr, "their AC": ac,
            "review": f["verdict"] or "unreviewed",
        })
    st.dataframe(pd.DataFrame(fdf), hide_index=True, use_container_width=True,
                 height=min(360, 80 + 35 * len(fdf)))
    others = sorted({e for f in flags if (e := _other(f)[1])})
    if others:
        st.caption("Open any linked person by pasting their EPIC into the "
                   "search box, then click their row.")


# ---- profile panel (top of the results area, when one is selected) --------
if st.session_state.get("exp_epic"):
    with st.container(border=True):
        render_profile(st.session_state["exp_epic"])
    st.divider()

st.caption(f"Showing **{start:,}–{end:,}** of **{total:,}** · page "
           f"**{page}** of **{pages}** · {PAGE_SIZE} per page · "
           "click a row (Table) or **View** (Gallery) to open a full profile")
_pager("top", with_jump=True)

# ---------------------------------------------------------------- results
if view == "Table":
    df = pd.DataFrame(rows)
    event = st.dataframe(df, use_container_width=True, hide_index=True,
                         height=560, key="exp_table", on_select="rerun",
                         selection_mode="single-row")
    picked = None
    if event and event.selection and event.selection.rows:
        idx = event.selection.rows[0]
        if idx < len(rows):
            picked = rows[idx]["epic_no"]
    if picked and picked != st.session_state.get("exp_epic"):
        st.session_state["exp_epic"] = picked
        st.rerun()
else:
    photos = get_photos([r["id"] for r in rows])
    per_row = 5
    for i in range(0, len(rows), per_row):
        cols = st.columns(per_row)
        for col, r in zip(cols, rows[i:i + per_row]):
            with col:
                img = photos.get(r["id"])
                if img:
                    st.image(img, use_container_width=True)
                else:
                    st.caption("_no photo_")
                badge = {"Found": "✅", "Not found": "❌"}.get(
                    r["epic_lookup_status"], "")
                st.markdown(f"**{r['name']}** {badge}")
                st.caption(f"{r['epic_no']} · AC {r['constituency_no']}/"
                           f"P{r['part_no']}/#{r['serial_no']}\n\n"
                           f"{r['gender']}, age {r['age']} · {r['relation_type']} "
                           f"{r['relation_name']}")
                st.button("🔍 View", key=f"view_{r['id']}",
                          on_click=_pick_epic, args=(r["epic_no"],),
                          use_container_width=True)

_pager("bottom", with_jump=False)

# ---------------------------------------------------------------- export
st.divider()
with st.expander("⬇️ Export matches to CSV"):
    st.caption("The current page, or all matches up to a safety cap of 5,000 "
               "rows.")
    e1, e2 = st.columns(2)
    page_csv = pd.DataFrame(rows).to_csv(index=False).encode()
    e1.download_button(f"This page ({len(rows)} rows)", page_csv,
                       file_name=f"voters_page{page}.csv", mime="text/csv",
                       use_container_width=True)
    if e2.button("Build CSV of all matches (≤5,000)", use_container_width=True):
        allrows = explore.export_rows(flt, sort=sort, limit=5000)
        buf = io.BytesIO()
        pd.DataFrame(allrows).to_csv(buf, index=False)
        st.download_button(f"Download {len(allrows):,} rows", buf.getvalue(),
                           file_name="voters_filtered.csv", mime="text/csv",
                           use_container_width=True)
        if total > 5000:
            st.caption(f"⚠️ Capped at 5,000 of {total:,} matches. Narrow the "
                       "filters to export a specific slice.")
