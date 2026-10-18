"""Create and populate the Google Sheet tabs from app/seed.py.

    python scripts/seed_sheet.py            # push Customers, Claims, Interactions
    python scripts/seed_sheet.py --csv out  # just write CSVs to import by hand

Rebuilding demo data is one command, so a rehearsal that mangles a row (or a
live demo that needs a clean slate) costs seconds. `--keep-interactions` leaves
the post-call log alone.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app.config import settings  # noqa: E402
from app.seed import CLAIMS, CUSTOMERS, INTERACTION_HEADERS  # noqa: E402

CUSTOMER_HEADERS = ["phone", "full_name", "preferred_name", "date_of_birth", "policy_id", "email"]
CLAIM_HEADERS = [
    "claim_id", "phone", "claim_type", "status", "status_detail", "amount",
    "adjuster_name", "required_documents", "submission_deadline", "last_updated", "next_step",
]


def rows(records, headers):
    return [headers] + [[str(record.get(h, "")) for h in headers] for record in records]


def write_csvs(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (
        ("customers.csv", rows(CUSTOMERS, CUSTOMER_HEADERS)),
        ("claims.csv", rows(CLAIMS, CLAIM_HEADERS)),
        ("interactions.csv", [INTERACTION_HEADERS]),
    ):
        with (out_dir / name).open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(data)
        print(f"  wrote {out_dir / name}")


def push(keep_interactions: bool) -> None:
    from app.repositories.sheets import SheetsRepository

    repo = SheetsRepository()
    book = repo._sheet()  # noqa: SLF001 - this script is the admin surface
    cfg = settings()
    print(f"spreadsheet: {book.title}")

    plan = [
        (cfg.customers_tab, rows(CUSTOMERS, CUSTOMER_HEADERS)),
        (cfg.claims_tab, rows(CLAIMS, CLAIM_HEADERS)),
    ]
    if not keep_interactions:
        plan.append((cfg.interactions_tab, [INTERACTION_HEADERS]))

    for tab, data in plan:
        try:
            worksheet = book.worksheet(tab)
            worksheet.clear()
        except Exception:
            worksheet = book.add_worksheet(
                title=tab, rows=max(100, len(data) + 50), cols=max(12, len(data[0]))
            )
        worksheet.update(values=data, range_name="A1", value_input_option="USER_ENTERED")
        worksheet.freeze(rows=1)
        print(f"  {tab}: {len(data) - 1} row(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", metavar="DIR", help="write CSV files instead of calling the Sheets API")
    parser.add_argument("--keep-interactions", action="store_true", help="do not clear the Interactions tab")
    args = parser.parse_args()

    if args.csv:
        write_csvs(Path(args.csv))
        return 0
    if not settings().sheet_id:
        print("SHEET_ID is not set. Either fill .env or use --csv out", file=sys.stderr)
        return 2
    push(args.keep_interactions)
    print("\nseeded. Scenario phone numbers:")
    print("  555-234-1187 Daniel Okafor  happy path (claim in review)")
    print("  555-361-2234 Priya Raman    documents required")
    print("  555-418-4492 Marcus Webb    auth failure (give a wrong DOB)")
    print("  555-572-5513 Lena Fischer   no claims on file")
    print("  555-693-6678 Ana Duarte     denied claim -> escalation")
    print("  555-999-8888 (nobody)       customer not found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
