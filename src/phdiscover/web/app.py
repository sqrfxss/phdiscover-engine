"""PhDiscover Web Interface — Streamlit app for browsing PhD positions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit as st

from phdiscover.models.discovery import MatchScore, Position

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
POSITIONS_FILE = DATA_DIR / "ranked_opportunities.json"
PI_FILE = DATA_DIR / "pi_database.json"


def load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def render_position(pos: dict[str, Any]) -> None:
    with st.container():
        st.markdown(f"### {pos.get('title', 'Untitled')}")
        col1, col2 = st.columns([2, 1])
        with col1:
            st.write(f"**University:** {pos.get('university', 'N/A')}")
            st.write(f"**Country:** {pos.get('country', 'N/A')}")
            st.write(f"**PI:** {pos.get('pi_name', 'N/A')}")
            st.write(f"**Funding:** {pos.get('funding', 'N/A')}")
            deadline = pos.get('application_deadline')
            if deadline:
                st.write(f"**Deadline:** {deadline}")
            st.write(f"**Type:** {pos.get('opportunity_type', 'N/A')}")
            fit = pos.get('research_fit_score', 0)
            st.progress(fit / 100, text=f"Research Fit: {fit:.0f}/100")
            priority = pos.get('contact_priority', 'LOW')
            color = {"HIGH": "green", "MEDIUM": "orange", "LOW": "gray"}.get(priority, "gray")
            st.markdown(f":{color}[Contact Priority: {priority}]")
            url = pos.get('url', '')
            if url:
                st.link_button("🔗 View Position", url)
            evidence = pos.get('evidence', [])
            if evidence:
                with st.expander("📋 Evidence"):
                    for ev in evidence:
                        st.write(f"- {ev.get('claim', '')}: {ev.get('value', '')} ({ev.get('source_type', '')})")
        with col2:
            email = pos.get('pi_email', '')
            if email:
                st.write(f"📧 {email}")
            st.write(f"Source: {pos.get('source_url', 'N/A')[:50]}...")
            if st.button(f"Export this", key=f"export_{pos.get('id', 'x')}"):
                st.download_button(
                    "Download CSV",
                    data=f"id,title,university,country,pi_name,pi_email,funding,deadline,url\n{pos.get('id','')},{pos.get('title','')},{pos.get('university','')},{pos.get('country','')},{pos.get('pi_name','')},{pos.get('pi_email','')},{pos.get('funding','')},{pos.get('application_deadline','')},{pos.get('url','')}",
                    file_name="position.csv",
                )


def main() -> None:
    st.set_page_config(
        page_title="PhDiscover — PhD Position Discovery",
        page_icon="🎓",
        layout="wide",
    )

    st.title("🎓 PhDiscover — PhD Position Discovery Engine")
    st.markdown("PI-first discovery · Evidence-backed verification · Zero-cost infrastructure")

    positions = load_json(POSITIONS_FILE)
    pis = load_json(PI_FILE)

    if not positions:
        st.info("No positions discovered yet. Run the crawler to populate data.")
        return

    st.sidebar.header("Filters")
    countries = sorted({p.get("country", "") for p in positions if p.get("country")})
    selected_countries = st.sidebar.multiselect("Country", countries, default=countries)
    fields = sorted({p.get("research_domain", "") for p in positions if p.get("research_domain")})
    selected_fields = st.sidebar.multiselect("Research Field", fields, default=fields)
    funding_only = st.sidebar.checkbox("Funding Required Only", value=True)
    min_fit = st.sidebar.slider("Minimum Research Fit", 0, 100, 50)
    priority_filter = st.sidebar.multiselect(
        "Contact Priority", ["HIGH", "MEDIUM", "LOW"], default=["HIGH", "MEDIUM", "LOW"]
    )

    filtered = []
    for p in positions:
        if selected_countries and p.get("country") not in selected_countries:
            continue
        if selected_fields and p.get("research_domain") not in selected_fields:
            continue
        if funding_only and not p.get("funding"):
            continue
        if p.get("research_fit_score", 0) < min_fit:
            continue
        if p.get("contact_priority") not in priority_filter:
            continue
        filtered.append(p)

    st.write(f"Showing **{len(filtered)}** of **{len(positions)}** positions")

    for pos in filtered:
        render_position(pos)

    st.markdown("---")
    st.write(f"**{len(pis)}** verified PIs · **{len(positions)}** positions · Daily crawl via GitHub Actions")


if __name__ == "__main__":
    main()