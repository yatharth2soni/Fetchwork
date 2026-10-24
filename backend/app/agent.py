"""uAgent entrypoint: construct the agent, register handlers, and run."""

import logging
from scripts.spike_agent import main

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
