"""Streamlit panel for uploading and previewing IQVIA report_sources TSV files."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from apps.core.paths import DEFAULT_REPORT_SOURCES_PATH
from apps.scrapers.iqvia.report_sources import (
    get_report_sources_info,
    load_report_sources,
    save_report_sources_upload,
    validate_report_sources_bytes,
)


def render_report_sources_panel(*, disabled: bool = False) -> None:
    """Show current product list file and allow uploading a replacement."""
    info = get_report_sources_info()
    active_path = Path(info["path"])

    with st.expander("Product list (report_sources.tsv)", expanded=True):
        if not info["exists"]:
            st.warning(
                "No product list file yet. Upload a `.tsv`, `.csv`, or `.xlsx` with columns: "
                "`Data Source`, `Cube No.`, `MARKET`, `PRODUCT` — or place "
                f"`{DEFAULT_REPORT_SOURCES_PATH.name}` under `data/iqvia/`."
            )
        elif info["source"] == "saved":
            label = info.get("original_filename") or active_path.name
            uploaded_at = info.get("uploaded_at")
            when = ""
            if uploaded_at:
                try:
                    when = datetime.fromisoformat(uploaded_at).strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    when = uploaded_at
            if info.get("bootstrapped_from_default"):
                detail = (
                    f"Saved from `{DEFAULT_REPORT_SOURCES_PATH.name}` "
                    f"({info['row_count']} product(s)). "
                )
            else:
                detail = (
                    f"Using saved file **{label}** "
                    f"({info['row_count']} product(s)"
                    f"{f', saved {when}' if when else ''}). "
                )
            st.success(
                detail + "Runs keep using this list until you upload a new file."
            )
        else:
            st.info(
                f"Using default file `{active_path.name}` "
                f"({info['row_count']} product(s)). "
                "Upload below to switch to a different list."
            )

        uploaded = st.file_uploader(
            "Upload new product list",
            type=["tsv", "txt", "csv", "xlsx", "xls"],
            disabled=disabled,
            help=(
                "Accepted formats: TSV, CSV, or Excel (.xlsx). "
                "Required columns: Data Source, Cube No., MARKET, PRODUCT. "
                "The file is converted to TSV and kept until you upload a new one."
            ),
            key="report_sources_uploader",
        )
        if uploaded is not None and not disabled:
            try:
                rows = validate_report_sources_bytes(
                    uploaded.getvalue(),
                    original_filename=uploaded.name,
                )
            except (OSError, ValueError) as exc:
                st.error(f"Could not use this file: {exc}")
            else:
                st.caption(f"Preview — {len(rows)} row(s) ready to save")
                st.dataframe(
                    [
                        {
                            "Data Source": row.data_source,
                            "Cube No.": row.cube_no,
                            "MARKET": row.market,
                            "PRODUCT": row.product,
                        }
                        for row in rows[:20]
                    ],
                    use_container_width=True,
                )
                if len(rows) > 20:
                    st.caption(f"…and {len(rows) - 20} more row(s)")
                if st.button(
                    "Save as active product list",
                    type="primary",
                    disabled=disabled,
                    key="save_report_sources_upload",
                ):
                    save_report_sources_upload(
                        uploaded.getvalue(),
                        original_filename=uploaded.name,
                    )
                    st.success(
                        f"Saved **{uploaded.name}** as TSV — download runs will use this list "
                        "until you upload another file."
                    )
                    st.rerun()

        if info["exists"] and not disabled:
            try:
                preview_rows = load_report_sources(active_path)
            except (OSError, ValueError) as exc:
                st.error(f"Active file has a problem: {exc}")
            else:
                with st.popover("View current product list"):
                    st.dataframe(
                        [
                            {
                                "Data Source": row.data_source,
                                "Cube No.": row.cube_no,
                                "MARKET": row.market,
                                "PRODUCT": row.product,
                            }
                            for row in preview_rows
                        ],
                        use_container_width=True,
                        height=400,
                    )
