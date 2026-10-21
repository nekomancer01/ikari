"""Gather subreddit posts/comments and score their sentiment."""
import sqlite3
import sys

__version__ = "0.4.0"

# Search (store.Search) needs SQLite 3.43 or later. Python on Linux uses the system's SQLite, older on many
# (Ubuntu 22.04's is 3.37), so there every `import sqlite3` gets pysqlite3-binary's newer one instead.
if sqlite3.sqlite_version_info < (3, 43):
    try:
        import pysqlite3
    except ImportError:  # not installed for this platform: search stays off (Store.upgrade stops at version 3)
        pass
    else:
        sys.modules["sqlite3"] = pysqlite3
