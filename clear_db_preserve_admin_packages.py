#!/usr/bin/env python3
"""
Clear database records while preserving admin and package tables when requested.

Usage:
	python clear_db_preserve_admin_packages.py --yes
	python clear_db_preserve_admin_packages.py --yes --keep-admin --preserve-packages

This script mirrors the behavior of clear_db.py but adds an option to preserve
any table whose name contains the substring "package" (case-insensitive).
"""

import argparse
from sqlalchemy import inspect, text

from app import app, db


PROTECTED_TABLES = {"alembic_version"}
ADMIN_TABLES = {"admin_users", "admin_logs"}


def get_table_names(connection):
	"""Return all table names from the active database."""
	return inspect(connection).get_table_names()


def clear_postgresql(connection, tables):
	"""Truncate PostgreSQL tables and reset identity values."""
	if not tables:
		return

	quoted_tables = ", ".join(f'"{table}"' for table in tables)
	connection.execute(text(f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY CASCADE"))


def clear_sqlite(connection, tables):
	"""Delete rows from SQLite tables and reset sqlite sequence values."""
	if not tables:
		return

	connection.execute(text("PRAGMA foreign_keys = OFF"))
	for table in tables:
		connection.execute(text(f'DELETE FROM "{table}"'))

	# Reset auto-increment counters if sqlite_sequence exists.
	try:
		connection.execute(text("DELETE FROM sqlite_sequence"))
	except Exception:
		pass
	finally:
		connection.execute(text("PRAGMA foreign_keys = ON"))


def clear_database(keep_admin=False, preserve_packages=False):
	"""Clear all rows in app tables while keeping structure intact.

	If `preserve_packages` is True, any table whose name contains
	'package' (case-insensitive) will be excluded from clearing.
	"""
	with app.app_context():
		dialect = db.engine.dialect.name

		with db.engine.begin() as connection:
			all_tables = set(get_table_names(connection))

			excluded_tables = set(PROTECTED_TABLES)
			if keep_admin:
				excluded_tables.update(ADMIN_TABLES)

			if preserve_packages:
				pkg_tables = {t for t in all_tables if 'package' in t.lower()}
				excluded_tables.update(pkg_tables)

			tables_to_clear = sorted(table for table in all_tables if table not in excluded_tables)

			if not tables_to_clear:
				print("No tables to clear.")
				return

			if dialect == "postgresql":
				clear_postgresql(connection, tables_to_clear)
			elif dialect == "sqlite":
				clear_sqlite(connection, tables_to_clear)
			else:
				# Generic fallback: try DELETE statements table-by-table.
				for table in tables_to_clear:
					connection.execute(text(f'DELETE FROM "{table}"'))

			print(f"Database cleared. {len(tables_to_clear)} table(s) cleaned.")
			if keep_admin or preserve_packages:
				preserved = [t for t in sorted(all_tables) if t in excluded_tables]
				print(f"Preserved tables: {', '.join(preserved)}")


def main():
	parser = argparse.ArgumentParser(description="Clear database records while keeping schema.")
	parser.add_argument(
		"--yes",
		action="store_true",
		help="Confirm destructive operation. Required to run.",
	)
	parser.add_argument(
		"--keep-admin",
		action="store_true",
		help="Keep admin tables (admin_users/admin_logs) unchanged.",
	)
	parser.add_argument(
		"--preserve-packages",
		action="store_true",
		help="Preserve any table whose name contains 'package' (case-insensitive).",
	)
	args = parser.parse_args()

	if not args.yes:
		print("Refusing to run without --yes. Example: python clear_db_preserve_admin_packages.py --yes")
		return

	clear_database(keep_admin=args.keep_admin, preserve_packages=args.preserve_packages)


if __name__ == "__main__":
	main()

