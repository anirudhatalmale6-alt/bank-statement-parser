"""bankstmt — read PDF bank statements into a SQLite database.

Layout
------
``extract.py``  PDF -> positioned text lines + column geometry
``dates.py``    date recognition and ISO conversion
``amounts.py``  money parsing (symbols, separators, CR/DR, parentheses)
``parser.py``   lines -> Transaction records + quality issues
``db.py``       SQLite schema and idempotent inserts
``report.py``   Markdown / CSV report of anything doubtful
``cli.py``      the ``python -m bankstmt`` command
"""

__version__ = "1.0.0"
