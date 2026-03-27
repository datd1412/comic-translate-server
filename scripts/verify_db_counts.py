import argparse
from sqlalchemy import create_engine, text


TABLES = ["users", "sessions", "payments", "credit_ledger"]


def table_counts(db_url: str) -> dict[str, int]:
    engine = create_engine(db_url)
    counts = {}
    with engine.connect() as conn:
        for table in TABLES:
            counts[table] = int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")) .scalar() or 0)
    return counts


def main():
    parser = argparse.ArgumentParser(description="Compare row counts between two databases.")
    parser.add_argument("--source-url", required=True, help="Source DB URL")
    parser.add_argument("--target-url", help="Target DB URL (optional)")
    args = parser.parse_args()

    src = table_counts(args.source_url)
    print("[source]")
    for t in TABLES:
        print(f"{t}: {src[t]}")

    if not args.target_url:
        return

    dst = table_counts(args.target_url)
    print("\n[target]")
    for t in TABLES:
        print(f"{t}: {dst[t]}")

    print("\n[diff]")
    ok = True
    for t in TABLES:
        diff = dst[t] - src[t]
        print(f"{t}: {diff:+d}")
        if diff != 0:
            ok = False

    print("\nResult:", "MATCH" if ok else "MISMATCH")


if __name__ == "__main__":
    main()
