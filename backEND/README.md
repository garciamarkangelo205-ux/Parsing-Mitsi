
# Additional Payables Parser

This Python/Streamlit application converts a wide accounting Excel report into:

- DATE
- REFERENCE
- PAYEE
- PARTICULARS
- BUS
- ACCOUNT CODE
- DEBIT
- CREDIT

## What it handles

- Report title rows above the real table
- Blank separator rows
- Duplicate `Reference` columns
- Account codes stored as horizontal columns, such as `701`, `703`, `711`, `401.1`, `799.1A`
- SUNDRY `ACCT / Dr / Cr`
- Date conversion
- Debit/credit cleanup
- Filters
- Balance diagnostics
- Clean Excel download

## Important accounting rule

The parser uses this default Chart-of-Accounts heuristic:

- `1xx`, `5xx`, `6xx`, `7xx`, `8xx`, `9xx` -> Debit
- `2xx`, `3xx`, `4xx` -> Credit

This matches examples such as expense accounts in the 7xx range appearing as debits and accounts like 401.1/403 appearing as credits.

If NKTI uses different rules for some account codes, edit these variables near the top of `app.py`:

```python
DEBIT_PREFIXES = ("1", "5", "6", "7", "8", "9")
CREDIT_PREFIXES = ("2", "3", "4")
```

For production use, it is better to replace the prefix heuristic with an official account-code-to-side mapping from your company's Chart of Accounts.

## Run

Open Command Prompt / PowerShell in this folder.

```bash
pip install -r requirements.txt
streamlit run app.py
```

Streamlit will give you a local URL, usually:

```text
http://localhost:8501
```

Upload the original `.xlsx` file and select the proper sheet.
