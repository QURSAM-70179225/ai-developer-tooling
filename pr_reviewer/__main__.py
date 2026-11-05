"""Allow `python -m pr_reviewer ...`."""

import sys

from .cli import main

sys.exit(main())
