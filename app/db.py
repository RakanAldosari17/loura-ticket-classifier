"""Every SQL statement in the system (section 5 of DESIGN.md).

No ORM and no query builder: plain `sqlite3` with `?` placeholders. Keeping all
SQL in one module means a reviewer can read the whole storage layer at once, and
the atomic claim that stops two workers taking the same ticket lives here.
"""
