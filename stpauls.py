#!/usr/bin/env python3
"""St. Paul's scraper entrypoint.

This intentionally stays separate from main.py, which remains TigerNet-first.
"""

from src.stpauls.cli import main


if __name__ == "__main__":
    main()
