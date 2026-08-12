"""Allow ``python -m tt_dlp`` to behave like the ``tt-dlp`` command."""

from .cli import main


raise SystemExit(main())
