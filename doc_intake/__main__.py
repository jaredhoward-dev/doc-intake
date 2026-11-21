"""`python3 -m doc_intake`: the minimal CLI over a config file (cli.py), naming itself that way in help and hints."""
import sys

from .cli import MODULE, main

if __name__ == "__main__":
    sys.exit(main(prog=MODULE))
