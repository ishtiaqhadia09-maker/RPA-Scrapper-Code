"""Run Iqvia bot from command line."""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from apps.core.paths import ensure_data_dirs
from apps.scrapers.iqvia.iqvia_bot import IqviaBot


def main():
    ensure_data_dirs()
    bot = IqviaBot()
    try:
        bot.run()
    finally:
        bot.close()


if __name__ == "__main__":
    main()
