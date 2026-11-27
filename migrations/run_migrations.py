"""Run SQL migrations in `migrations/` against the configured MySQL database."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text

from app.database.connection import engine


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into executable statements, ignoring comment-only blocks."""
    cleaned_lines = [
        line for line in sql.splitlines() if not line.strip().startswith("--")
    ]
    cleaned_sql = "\n".join(cleaned_lines)
    return [stmt.strip() for stmt in cleaned_sql.split(";") if stmt.strip()]


def run_migrations() -> None:
    migrations_dir = Path(__file__).resolve().parent
    sql_files = sorted(migrations_dir.glob("*.sql"))

    if not sql_files:
        print("No .sql migration files found.")
        return

    with engine.begin() as conn:
        for sql_file in sql_files:
            print(f"Applying {sql_file.name} ...")
            sql_content = sql_file.read_text(encoding="utf-8")
            statements = _split_sql_statements(sql_content)
            for stmt in statements:
                conn.execute(text(stmt))
            print(f"Applied {sql_file.name} successfully ({len(statements)} statements).")

    print("All migrations completed successfully.")


if __name__ == "__main__":
    run_migrations()
