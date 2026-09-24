"""One paper arm, for ``python -m kalshi_bot.paper_cli``.

The ``--ab`` supervisor starts two of these so each arm has its own lock,
cash, and learner. This module does not place orders.
"""

from kalshi_bot.cli import paper_main

if __name__ == "__main__":
    paper_main()
