"""``python -m cpgsim`` is the same as the ``sim`` command."""

import sys

from .cli import main

sys.exit(main())
