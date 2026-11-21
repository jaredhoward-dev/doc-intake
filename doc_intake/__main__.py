"""`python3 -m doc_intake`: the minimal CLI over a config file (cli.py)."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
