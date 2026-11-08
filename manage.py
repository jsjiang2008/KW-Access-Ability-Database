"""Local-only account administration and consistent database backups."""

import argparse
from getpass import getpass
from pathlib import Path

from auth import Auth
from storage import Database


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", help="Use the same persistent folder as server.py")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("create-user", "reset-password", "delete-user"):
        sub.add_parser(command).add_argument("username")
    sub.add_parser("list-users")
    sub.add_parser("backup").add_argument("destination")
    args = parser.parse_args(argv)
    database = Database(args.data_dir)
    try:
        if args.command == "backup":
            database.backup(args.destination)
            print(f"Backup saved to {Path(args.destination).resolve()}. Restored users must sign in again.")
        elif args.command == "list-users":
            with database.connect() as db:
                for row in db.execute("SELECT username FROM users ORDER BY username"):
                    print(row["username"])
        else:
            auth = Auth(database)
            if args.command == "delete-user":
                auth.delete_user(args.username)
                print("Account removed and its sessions revoked.")
            else:
                password = getpass("Password/passphrase (12–128 characters): ")
                if password != getpass("Confirm password: "):
                    raise ValueError("Passwords do not match.")
                auth.set_password(args.username, password, create=args.command == "create-user")
                print("Account saved. Any previous sessions for this user have been revoked.")
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
