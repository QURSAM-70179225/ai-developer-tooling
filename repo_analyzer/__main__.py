"""Allow `python -m repo_analyzer <url>`."""

import sys

from .cli import main

sys.exit(main())
