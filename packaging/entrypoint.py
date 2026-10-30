"""Keep the frozen entry outside scripts/ to avoid scripts/docqa.py shadowing docqa."""

from docqa.desktop import entry

if __name__ == "__main__":
    entry()
