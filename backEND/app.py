
import io
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st


# ============================================================
# CONFIGURATION
# ============================================================

OUTPUT_COLUMNS = [
    "DATE",
    "REFERENCE",
    "PAYEE",
    "PARTICULARS",
    "BUS",
    "ACCOUNT CODE",
    "DEBIT",
    "CREDIT",
]

# These are aliases the parser will recognize in the uploaded report.
HEADER_ALIASES = {
    "DATE": {"DATE", "TRANSACTION DATE", "DOC DATE"},
    "REFERENCE": {"REFERENCE", "REF", "REF NO", "REF NO.", "REFERENCE NO", "REFERENCE NUMBER"},
    "PAYEE": {"PAYEE", "PAYEE NAME", "VENDOR", "SUPPLIER"},
    "PARTICULARS": {"PARTICULAR", "PARTICULARS", "DESCRIPTION", "DETAILS"},
    "BUS": {"BUS", "BUS NO", "BUS NO.", "BUS NUMBER"},
}

# Common accounting-side heuristic:
# 1xx = assets -> normally debit
# 2xx = liabilities -> normally credit
# 3xx = equity -> normally credit
# 4xx = income/liability-style accounts in this report -> normally credit
# 5xx-9xx = expense/cost-style accounts -> normally debit
#
# IMPORTANT:
# This is a default rule and can be edited here to match your company's
# Chart of Accounts.
DEBIT_PREFIXES = ("1", "5", "6", "7", "8", "9")
CREDIT_PREFIXES = ("2", "3", "4")


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def clean_text(value) -> str:
    """Convert a cell to a trimmed string, but keep blanks empty."""
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return text


def normalize_header(value) -> str:
    """Normalize header text for easier matching."""
    text = clean_text(value).upper()
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_account_code(value) -> str:
    """
    Normalize account codes such as:
      701.0 -> 701
      401.1 -> 401.1
      799.1A -> 799.1A
    """
    if pd.isna(value):
        return ""

    if isinstance(value, (int, np.integer)):
        return str(int(value))

    if isinstance(value, (float, np.floating)):
        if float(value).is_integer():
            return str(int(value))
        return str(value).rstrip("0").rstrip(".")

    text = str(value).strip().upper()
    text = re.sub(r"\s+", "", text)
    return text


def is_account_code_header(value) -> bool:
    """
    Detect account-code-looking headers:
      701
      401.1
      799.1A
      760.1B
    """
    text = normalize_account_code(value)
    if not text:
        return False

    # Exclude things that are clearly not account codes.
    if text in {"DR", "CR", "ACCT", "ACCOUNT", "DATE"}:
        return False

    return bool(re.fullmatch(r"\d{2,4}(?:\.\d+)?[A-Z]?", text))


def parse_amount(value) -> float:
    """
    Convert Excel amount cells into float.
    Handles commas, parentheses, currency signs, and blanks.
    """
    if pd.isna(value):
        return 0.0

    if isinstance(value, (int, float, np.integer, np.floating)):
        if pd.isna(value):
            return 0.0
        return float(value)

    text = str(value).strip()
    if not text:
        return 0.0

    # Excel screenshot may show #####, but the real xlsx normally stores
    # the underlying number. If an actual text "####" is present, skip it.
    if set(text) == {"#"}:
        return 0.0

    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = text.replace(",", "")
    text = text.replace("₱", "")
    text = text.replace("$", "")
    text = text.strip()

    try:
        number = float(text)
        return -number if negative else number
    except ValueError:
        return 0.0


def infer_account_side(account_code: str) -> str:
    """
    Infer whether a matrix account code is debit or credit.

    You can customize DEBIT_PREFIXES / CREDIT_PREFIXES above if your
    company's Chart of Accounts uses different conventions.
    """
    code = normalize_account_code(account_code)

    if code.startswith(DEBIT_PREFIXES):
        return "DEBIT"

    if code.startswith(CREDIT_PREFIXES):
        return "CREDIT"

    # Safer fallback: unknown codes go to debit, but they will be
    # highlighted in validation so the user can review them.
    return "DEBIT"


def find_header_row(raw: pd.DataFrame, scan_rows: int = 40) -> int:
    """
    Find the row that contains the real transaction headers.
    We score rows based on DATE, REFERENCE, PAYEE, BUS, etc.
    """
    best_row = None
    best_score = -1

    max_scan = min(scan_rows, len(raw))

    for r in range(max_scan):
        values = [normalize_header(v) for v in raw.iloc[r].tolist()]
        values_set = set(values)

        score = 0
        for canonical, aliases in HEADER_ALIASES.items():
            if any(alias in values_set for alias in aliases):
                score += 2

        # Strong signals for the accounting report.
        if "DATE" in values_set:
            score += 3
        if "PAYEE" in values_set:
            score += 3
        if "BUS" in values_set:
            score += 2
        if "ACCT" in values_set:
            score += 1
        if "DR" in values_set:
            score += 1
        if "CR" in values_set:
            score += 1

        if score > best_score:
            best_score = score
            best_row = r

    if best_row is None or best_score < 5:
        raise ValueError(
            "Could not automatically locate the table header row. "
            "The file may use a different layout."
        )

    return best_row


def find_column_positions(headers: List[str]) -> Dict[str, List[int]]:
    """
    Return positions for known logical columns.
    Multiple REFERENCE columns are allowed.
    """
    result = {key: [] for key in HEADER_ALIASES}

    for idx, header in enumerate(headers):
        normalized = normalize_header(header)
        for canonical, aliases in HEADER_ALIASES.items():
            if normalized in aliases:
                result[canonical].append(idx)

    return result


def first_nonblank_from_positions(row: pd.Series, positions: List[int]) -> str:
    """Return the first nonblank value among duplicate header positions."""
    for pos in positions:
        value = clean_text(row.iloc[pos])
        if value:
            return value
    return ""


def parse_date_value(value):
    """Parse a date but preserve readable output."""
    if pd.isna(value) or clean_text(value) == "":
        return pd.NaT

    # Pandas handles Excel datetime objects and many string formats.
    parsed = pd.to_datetime(value, errors="coerce")

    # If strings such as 01082026 appear, try MMDDYYYY.
    if pd.isna(parsed):
        text = re.sub(r"\D", "", clean_text(value))
        if len(text) == 8:
            parsed = pd.to_datetime(text, format="%m%d%Y", errors="coerce")

    return parsed


def detect_sundry_columns(headers: List[str]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Find SUNDRY ACCT / Dr / Cr columns from the actual header row."""
    acct_idx = dr_idx = cr_idx = None

    normalized = [normalize_header(h) for h in headers]

    # Most reports have ACCT, Dr, Cr close together.
    for i, h in enumerate(normalized):
        if h in {"ACCT", "ACCOUNT", "ACCOUNT CODE"}:
            # Look nearby for DR / CR.
            nearby = range(max(0, i - 2), min(len(headers), i + 5))
            nearby_headers = {j: normalized[j] for j in nearby}

            possible_dr = [j for j, v in nearby_headers.items() if v in {"DR", "DEBIT"}]
            possible_cr = [j for j, v in nearby_headers.items() if v in {"CR", "CREDIT"}]

            if possible_dr and possible_cr:
                acct_idx = i
                dr_idx = possible_dr[0]
                cr_idx = possible_cr[0]
                break

    return acct_idx, dr_idx, cr_idx


def normalize_additional_payables(uploaded_file, sheet_name=0) -> Tuple[pd.DataFrame, dict]:
    """
    Convert a wide Additional Payables report into normalized rows.

    Output:
      DATE | REFERENCE | PAYEE | PARTICULARS | BUS |
      ACCOUNT CODE | DEBIT | CREDIT
    """
    raw = pd.read_excel(uploaded_file, sheet_name=sheet_name, header=None)

    if raw.empty:
        raise ValueError("The uploaded sheet is empty.")

    header_row = find_header_row(raw)
    headers = [clean_text(v) for v in raw.iloc[header_row].tolist()]
    data = raw.iloc[header_row + 1:].copy().reset_index(drop=True)

    positions = find_column_positions(headers)

    # Required columns
    required = ["DATE", "REFERENCE", "PAYEE", "BUS"]
    missing = [c for c in required if not positions[c]]
    if missing:
        raise ValueError(
            "Missing required column(s): " + ", ".join(missing)
        )

    sundry_acct_idx, sundry_dr_idx, sundry_cr_idx = detect_sundry_columns(headers)

    # Identify account-code matrix columns.
    account_cols = []
    for idx, header in enumerate(headers):
        if is_account_code_header(header):
            account_cols.append((idx, normalize_account_code(header)))

    output_rows = []
    warnings = []

    for _, row in data.iterrows():
        date_raw = first_nonblank_from_positions(row, positions["DATE"])
        reference = first_nonblank_from_positions(row, positions["REFERENCE"])
        payee = first_nonblank_from_positions(row, positions["PAYEE"])
        particulars = first_nonblank_from_positions(row, positions["PARTICULARS"])
        bus = first_nonblank_from_positions(row, positions["BUS"])

        # Skip totally empty/noise rows.
        if not any([date_raw, reference, payee, particulars, bus]):
            # Keep processing only if there are accounting amounts;
            # otherwise this is just a separator row.
            has_amount = False
            for idx, _ in account_cols:
                if parse_amount(row.iloc[idx]) != 0:
                    has_amount = True
                    break

            if not has_amount and sundry_dr_idx is not None and sundry_cr_idx is not None:
                has_amount = (
                    parse_amount(row.iloc[sundry_dr_idx]) != 0
                    or parse_amount(row.iloc[sundry_cr_idx]) != 0
                )

            if not has_amount:
                continue

        parsed_date = parse_date_value(date_raw)

        base = {
            "DATE": parsed_date,
            "REFERENCE": reference,
            "PAYEE": payee,
            "PARTICULARS": particulars,
            "BUS": bus,
        }

        # A) Matrix account-code columns
        for idx, account_code in account_cols:
            amount = parse_amount(row.iloc[idx])

            if amount == 0:
                continue

            side = infer_account_side(account_code)

            record = {
                **base,
                "ACCOUNT CODE": account_code,
                "DEBIT": abs(amount) if side == "DEBIT" else 0.0,
                "CREDIT": abs(amount) if side == "CREDIT" else 0.0,
            }
            output_rows.append(record)

        # B) SUNDRY section: ACCT / Dr / Cr
        if sundry_acct_idx is not None:
            account_code = normalize_account_code(row.iloc[sundry_acct_idx])

            if account_code:
                debit = parse_amount(row.iloc[sundry_dr_idx]) if sundry_dr_idx is not None else 0.0
                credit = parse_amount(row.iloc[sundry_cr_idx]) if sundry_cr_idx is not None else 0.0

                if debit != 0 or credit != 0:
                    output_rows.append({
                        **base,
                        "ACCOUNT CODE": account_code,
                        "DEBIT": abs(debit),
                        "CREDIT": abs(credit),
                    })

    result = pd.DataFrame(output_rows, columns=OUTPUT_COLUMNS)

    if result.empty:
        return result, {
            "header_row": header_row + 1,
            "account_columns": [code for _, code in account_cols],
            "warnings": ["No payable rows were created from the uploaded file."],
        }

    # Fill down transaction identity fields.
    # Some accounting reports show DATE/REFERENCE/PAYEE only on the first
    # line of a multi-line journal entry.
    fill_cols = ["DATE", "REFERENCE", "PAYEE", "PARTICULARS", "BUS"]
    result[fill_cols] = result[fill_cols].replace("", np.nan).ffill()

    # Numeric cleanup
    result["DEBIT"] = pd.to_numeric(result["DEBIT"], errors="coerce").fillna(0.0)
    result["CREDIT"] = pd.to_numeric(result["CREDIT"], errors="coerce").fillna(0.0)

    # Remove exact accidental duplicates.
    result = result.drop_duplicates().reset_index(drop=True)

    # Validation: one line should not normally have both debit and credit.
    both = result[(result["DEBIT"] != 0) & (result["CREDIT"] != 0)]
    if not both.empty:
        warnings.append(
            f"{len(both)} row(s) contain both a debit and a credit. Please review them."
        )

    # Validation: references should balance where possible.
    balance = (
        result.groupby("REFERENCE", dropna=False)[["DEBIT", "CREDIT"]]
        .sum()
        .reset_index()
    )
    balance["DIFFERENCE"] = (balance["DEBIT"] - balance["CREDIT"]).round(2)

    unbalanced = balance[
        (balance["REFERENCE"].astype(str).str.strip() != "")
        & (balance["DIFFERENCE"].abs() > 0.01)
    ]

    if not unbalanced.empty:
        warnings.append(
            f"{len(unbalanced)} reference(s) are not balanced using the default "
            "account-side rules. Review the Chart of Accounts mapping."
        )

    diagnostics = {
        "header_row": header_row + 1,  # Excel-style 1-based
        "account_columns": [code for _, code in account_cols],
        "warnings": warnings,
        "balance": balance,
        "unbalanced": unbalanced,
    }

    return result, diagnostics


def dataframe_to_excel_bytes(df: pd.DataFrame) -> bytes:
    """Create a clean Excel file in memory."""
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export_df = df.copy()

        # Keep actual datetime values in Excel.
        export_df.to_excel(writer, index=False, sheet_name="Additional Payables")

        ws = writer.book["Additional Payables"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

        widths = {
            "A": 15, "B": 16, "C": 30, "D": 55,
            "E": 25, "F": 16, "G": 16, "H": 16
        }
        for col, width in widths.items():
            ws.column_dimensions[col].width = width

        for cell in ws["G"][1:]:
            cell.number_format = '#,##0.00'
        for cell in ws["H"][1:]:
            cell.number_format = '#,##0.00'

    return output.getvalue()


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(
    page_title="Additional Payables Parser",
    page_icon="📊",
    layout="wide",
)

st.markdown(
    """
    <style>
        .stApp {
            background-color: #f5f8fa;
        }
        .main-title {
            font-size: 24px;
            font-weight: 700;
            margin-bottom: 4px;
        }
        .subtle {
            color: #6b7280;
            font-size: 14px;
        }
        .panel {
            background: white;
            border: 1px solid #e5e7eb;
            border-radius: 14px;
            padding: 18px;
            margin-bottom: 14px;
        }
        div[data-testid="stMetric"] {
            background: white;
            border: 1px solid #e5e7eb;
            padding: 14px;
            border-radius: 12px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="main-title">Additional Payables</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtle">Upload the original wide Excel report and automatically convert it into a normalized, readable table.</div>',
    unsafe_allow_html=True,
)

st.write("")

uploaded_file = st.file_uploader(
    "Upload Additional Payables Excel file",
    type=["xlsx", "xls"],
    help="The parser will locate the real header row automatically.",
)

if uploaded_file is None:
    st.info("Upload an Excel file to begin.")
    st.stop()

# Detect sheet names first.
try:
    excel_file = pd.ExcelFile(uploaded_file)
    sheet_names = excel_file.sheet_names
except Exception as exc:
    st.error(f"Could not open the Excel file: {exc}")
    st.stop()

selected_sheet = st.selectbox("Sheet", sheet_names)

# Rewind because ExcelFile may consume the uploaded stream.
uploaded_file.seek(0)

try:
    parsed_df, diagnostics = normalize_additional_payables(
        uploaded_file,
        sheet_name=selected_sheet,
    )
except Exception as exc:
    st.error(f"Parsing failed: {exc}")
    st.stop()

if parsed_df.empty:
    st.warning("The parser did not find any payable entries.")
    st.stop()

# ---------------- FILTERS ----------------

st.write("")
st.subheader("Filters")

f1, f2, f3, f4 = st.columns([1, 1, 1, 1])

min_date = parsed_df["DATE"].dropna().min()
max_date = parsed_df["DATE"].dropna().max()

with f1:
    date_from = st.date_input(
        "DATE FROM",
        value=min_date.date() if pd.notna(min_date) else None,
    )

with f2:
    date_to = st.date_input(
        "DATE TO",
        value=max_date.date() if pd.notna(max_date) else None,
    )

with f3:
    reference_filter = st.text_input("REFERENCE", placeholder="Filter Reference")

with f4:
    payee_filter = st.text_input("PAYEE", placeholder="Filter Payee")

f5, f6, f7 = st.columns([1, 1, 2])

with f5:
    bus_filter = st.text_input("BUS", placeholder="Filter BUS")

with f6:
    acct_filter = st.text_input("ACCOUNT CODE", placeholder="Filter Account Code")

with f7:
    particulars_filter = st.text_input("PARTICULARS", placeholder="Filter Particulars")


filtered = parsed_df.copy()

if date_from:
    filtered = filtered[
        filtered["DATE"].isna()
        | (filtered["DATE"].dt.date >= date_from)
    ]

if date_to:
    filtered = filtered[
        filtered["DATE"].isna()
        | (filtered["DATE"].dt.date <= date_to)
    ]

def apply_contains(df, column, value):
    if value:
        return df[
            df[column].astype(str).str.contains(
                value,
                case=False,
                na=False,
                regex=False,
            )
        ]
    return df

filtered = apply_contains(filtered, "REFERENCE", reference_filter)
filtered = apply_contains(filtered, "PAYEE", payee_filter)
filtered = apply_contains(filtered, "BUS", bus_filter)
filtered = apply_contains(filtered, "ACCOUNT CODE", acct_filter)
filtered = apply_contains(filtered, "PARTICULARS", particulars_filter)

# ---------------- METRICS ----------------

m1, m2, m3, m4 = st.columns(4)

with m1:
    st.metric("Parsed Rows", f"{len(filtered):,}")
with m2:
    st.metric("Total Debit", f"{filtered['DEBIT'].sum():,.2f}")
with m3:
    st.metric("Total Credit", f"{filtered['CREDIT'].sum():,.2f}")
with m4:
    diff = filtered["DEBIT"].sum() - filtered["CREDIT"].sum()
    st.metric("Difference", f"{diff:,.2f}")

# ---------------- TABLE ----------------

display_df = filtered.copy()
display_df["DATE"] = display_df["DATE"].dt.strftime("%b %d, %Y")
display_df["DEBIT"] = display_df["DEBIT"].map(lambda x: f"{x:,.2f}")
display_df["CREDIT"] = display_df["CREDIT"].map(lambda x: f"{x:,.2f}")

st.write("")
st.dataframe(
    display_df,
    use_container_width=True,
    hide_index=True,
    height=520,
)

# ---------------- DOWNLOAD ----------------

excel_bytes = dataframe_to_excel_bytes(filtered)

st.download_button(
    "Download Cleaned Excel",
    data=excel_bytes,
    file_name="additional_payables_cleaned.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

# ---------------- DIAGNOSTICS ----------------

with st.expander("Parser diagnostics"):
    st.write(f"Detected header row: **{diagnostics['header_row']}**")
    st.write(
        "Detected matrix account codes: "
        + ", ".join(diagnostics["account_columns"])
    )

    if diagnostics["warnings"]:
        for warning in diagnostics["warnings"]:
            st.warning(warning)
    else:
        st.success("No parser warnings.")

    if "unbalanced" in diagnostics and not diagnostics["unbalanced"].empty:
        st.write("Unbalanced references:")
        st.dataframe(
            diagnostics["unbalanced"],
            use_container_width=True,
            hide_index=True,
        )
