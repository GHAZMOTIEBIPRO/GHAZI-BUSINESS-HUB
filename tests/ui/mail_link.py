"""Print the newest link with a given path prefix that was e-mailed to an address (test servers only).

usage: python tests/ui/mail_link.py <sqlite_db_path> <email> </verify/|/reset/>
"""
import re
import sqlite3
import sys

db, email, prefix = sys.argv[1:4]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
for (body,) in con.execute("SELECT body FROM email_outbox WHERE \"to\" = ? ORDER BY id DESC", (email,)):
    m = re.search(re.escape(prefix) + r"[\w-]+", body)
    if m:
        print(m.group(0))
        sys.exit(0)
sys.exit(f"no {prefix} link for {email}")
