"""Diagnose the Google Sheets integration, one failure mode at a time.

    python scripts/check_sheets.py            # read-only checks
    python scripts/check_sheets.py --write    # also append and delete a probe row

Every check prints the exact fix for its own failure, because the four ways
this integration breaks (key missing, API not enabled, sheet not shared, tab
misnamed) all surface as a vague 403 or 404 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app.config import settings  # noqa: E402
from app.models import InteractionRecord, normalize_phone  # noqa: E402

OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"


def fail(message: str, fix: str) -> None:
    print(f"{BAD} {message}")
    for line in fix.strip().splitlines():
        print(f"         {line.strip()}")
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="append and then remove a probe row")
    args = parser.parse_args()

    cfg = settings()
    print(f"\nBACKEND={cfg.backend}  SHEET_ID={cfg.sheet_id[:12] + '…' if cfg.sheet_id else '(empty)'}\n")

    # 1 — credentials present and parseable
    if not (cfg.google_credentials_json.strip() or cfg.google_credentials_file.strip()):
        fail("no service-account credentials", """
            Set one of these in .env:
              GOOGLE_CREDENTIALS_FILE=secrets/service-account.json
              GOOGLE_CREDENTIALS_JSON={"type":"service_account",...}   (one line, for Render)
        """)

    from app.repositories.sheets import SheetsRepository, describe_error

    repo = SheetsRepository()
    try:
        creds = repo._credentials()  # noqa: SLF001 - this script is the diagnostic surface
    except json.JSONDecodeError:
        fail("GOOGLE_CREDENTIALS_JSON is not valid JSON", """
            It must be the whole key file on ONE line, including the \\n escapes
            inside private_key. Easiest: use GOOGLE_CREDENTIALS_FILE locally.
        """)
    except FileNotFoundError:
        fail(f"key file not found: {cfg.google_credentials_file}", """
            Path is relative to the project root. Check the filename.
        """)
    except Exception as exc:  # noqa: BLE001
        fail(f"could not load credentials: {exc}", "Re-download the JSON key from Google Cloud.")

    email = getattr(creds, "service_account_email", "unknown")
    print(f"{OK} credentials load")
    print(f"         service account: {email}")

    if not cfg.sheet_id:
        fail("SHEET_ID is empty", """
            Open your sheet. The URL is
              docs.google.com/spreadsheets/d/<THIS PART>/edit
            Put <THIS PART> in .env as SHEET_ID.
        """)

    # 2 — API enabled, sheet exists, sheet shared
    try:
        book = repo._sheet()  # noqa: SLF001
        title = book.title
    except Exception as exc:  # noqa: BLE001
        # gspread raises a bare PermissionError/SpreadsheetNotFound with an
        # empty message, so classify on the flattened cause chain, not str(exc).
        text = describe_error(exc)
        print(f"         raw error: {text[:400]}")
        if "PermissionError" in text or "does not have permission" in text:
            fail("the service account cannot open this sheet", f"""
                Open the sheet > Share > add this address and set it to Editor:
                  {email}
                Untick "Notify people", then press Share (not just Copy link).
                Also confirm SHEET_ID belongs to the sheet you shared.
            """)
        if "has not been used in project" in text or "SERVICE_DISABLED" in text:
            fail("the Google Sheets API is not enabled on this project", """
                Google Cloud console > APIs & Services > Library
                Enable BOTH: "Google Sheets API" and "Google Drive API"
                Wait ~30 seconds, then re-run.
            """)
        if "PERMISSION_DENIED" in text or "403" in text:
            fail("the service account cannot open this sheet", f"""
                Open the sheet > Share > add this address as an Editor:
                  {email}
                Untick "Notify people". Then re-run.
            """)
        if "404" in text or "not found" in text.lower():
            fail("no sheet with that SHEET_ID", """
                Copy the id out of the sheet URL again — it is the part between
                /d/ and /edit, with no slashes.
            """)
        fail(f"could not open the spreadsheet: {text[:300]}", "See the message above.")

    print(f"{OK} opened spreadsheet: {title!r}")

    # 3 — tabs
    tabs = [ws.title for ws in book.worksheets()]
    print(f"         tabs: {', '.join(tabs)}")
    missing = [t for t in (cfg.customers_tab, cfg.claims_tab) if t not in tabs]
    if missing:
        fail(f"missing tab(s): {', '.join(missing)}", """
            Run:  python scripts/seed_sheet.py
            (it creates and fills every tab), or rename the tabs to match .env.
        """)
    print(f"{OK} required tabs present")
    if cfg.interactions_tab not in tabs:
        print(f"{WARN} no {cfg.interactions_tab!r} tab yet — the first write-back will create it")

    # 4 — read + column mapping
    customers = repo.find_customer_by_phone("555-234-1187")
    rows = repo._records(cfg.customers_tab)  # noqa: SLF001
    print(f"{OK} read {len(rows)} customer row(s)")
    if not rows:
        fail("the Customers tab is empty", "Run: python scripts/seed_sheet.py")

    from app.repositories.sheets import CUSTOMER_ALIASES, _canon, _map_row

    mapped = _map_row(rows[0], CUSTOMER_ALIASES)
    unmapped = [h for h in rows[0] if not CUSTOMER_ALIASES.get(_canon(h))]
    print(f"         mapped columns: {', '.join(sorted(mapped)) or '(none)'}")
    if unmapped:
        print(f"{WARN} ignored columns: {', '.join(unmapped)}")
    for required in ("phone", "full_name", "date_of_birth"):
        if not mapped.get(required):
            fail(f"column for {required!r} is missing or empty in row 2", f"""
                Row 1 holds the headers. A header matching {required!r} must exist
                — see CUSTOMER_ALIASES in app/repositories/sheets.py for accepted
                spellings (e.g. phone / Phone Number / mobile).
            """)
    print(f"{OK} column mapping resolves phone, full_name, date_of_birth")

    if customers is None:
        print(f"{WARN} no customer matched 555-234-1187 — seed data may differ; that is fine")
    else:
        claims = repo.claims_for_phone(customers.phone)
        print(f"{OK} lookup works: {customers.full_name} with {len(claims)} claim(s)")
        if not claims:
            print(f"{WARN} no claims matched — check the Claims tab phone column "
                  f"(normalises to {normalize_phone(customers.phone)})")

    # 5 — write
    if args.write:
        probe = InteractionRecord(
            call_id="diagnostic-probe",
            caller_name="Diagnostic probe",
            summary="Written by scripts/check_sheets.py; safe to delete.",
            sentiment="neutral",
        )
        try:
            repo.write_interaction(probe)
        except Exception as exc:  # noqa: BLE001
            fail(f"write-back failed: {str(exc)[:300]}", f"""
                The service account needs EDITOR access, not Viewer.
                Re-share the sheet with {email} as an Editor.
            """)
        worksheet = book.worksheet(cfg.interactions_tab)
        column = [_canon(h) for h in worksheet.row_values(1)].index("callid") + 1
        found = [i for i, value in enumerate(worksheet.col_values(column), start=1)
                 if value == "diagnostic-probe"]
        print(f"{OK} write-back appended row {found[-1] if found else '?'} of {cfg.interactions_tab}")
        for row in reversed(found):
            worksheet.delete_rows(row)
        print(f"{OK} probe row removed (idempotency + delete both work)")

    print("\nall checks passed — set BACKEND=sheets and the agent is live on Sheets\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
