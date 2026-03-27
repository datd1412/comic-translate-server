import argparse
from sqlalchemy import MetaData, Table, create_engine, delete, select


TABLE_ORDER = [
    "users",
    "sessions",
    "payments",
    "credit_ledger",
]


def copy_table(src_engine, dst_engine, table_name: str, truncate: bool):
    src_table = Table(table_name, MetaData(), autoload_with=src_engine)
    dst_table = Table(table_name, MetaData(), autoload_with=dst_engine)

    with src_engine.connect() as src_conn:
        rows = [dict(row._mapping) for row in src_conn.execute(select(src_table))]

    if not rows:
        print(f"[skip] {table_name}: no rows")
        return

    with dst_engine.begin() as dst_conn:
        if truncate:
            dst_conn.execute(delete(dst_table))
        dst_conn.execute(dst_table.insert(), rows)

    print(f"[ok] {table_name}: copied {len(rows)} rows")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate data from SQLite database to PostgreSQL database."
    )
    parser.add_argument(
        "--sqlite-url",
        default="sqlite:///./comic_server.db",
        help="Source SQLite URL (default: sqlite:///./comic_server.db)",
    )
    parser.add_argument(
        "--postgres-url",
        required=True,
        help="Destination PostgreSQL URL, e.g. postgresql+psycopg2://user:pass@host:5432/dbname",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Delete existing rows in destination tables before copy.",
    )
    args = parser.parse_args()

    src_engine = create_engine(args.sqlite_url)
    dst_engine = create_engine(args.postgres_url)

    for table_name in TABLE_ORDER:
        copy_table(src_engine, dst_engine, table_name, args.truncate)

    print("Done.")


if __name__ == "__main__":
    main()
