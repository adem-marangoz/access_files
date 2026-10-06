"""Entry point: ``python agent.py [run|check|send|report|chat-id] [-c config.yaml]``."""

import sys

from tpms_agent.agent import main

if __name__ == "__main__":
    sys.exit(main())
