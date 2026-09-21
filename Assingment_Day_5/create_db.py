"""Create students.db and load the five student records from the problem statement.

Run this once before agent.py:

    python create_db.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "students.db"

STUDENTS = [
    ("22CS045", "Dhanushya", "Computer Science",       85, 72, 90, 78),
    ("22CS046", "Rahul",     "Computer Science",       65, 70, 68, 72),
    ("22CS047", "Priya",     "Information Technology", 92, 88, 95, 90),
    ("22CS048", "Arun",      "Information Technology", 55, 60, 58, 62),
    ("22CS049", "Meena",     "Computer Science",       78, 85, 80, 88),
]


def create_database() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS students")
    cur.execute(
        """
        CREATE TABLE students (
            student_id TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            department TEXT NOT NULL,
            python     INTEGER NOT NULL,
            database   INTEGER NOT NULL,
            ai         INTEGER NOT NULL,
            web        INTEGER NOT NULL
        )
        """
    )
    cur.executemany(
        "INSERT INTO students VALUES (?, ?, ?, ?, ?, ?, ?)",
        STUDENTS,
    )
    conn.commit()

    print(f"Created {DB_PATH}")
    print(f"Inserted {cur.execute('SELECT COUNT(*) FROM students').fetchone()[0]} rows:\n")
    for row in cur.execute("SELECT * FROM students"):
        print("   ", row)
    conn.close()


if __name__ == "__main__":
    create_database()
