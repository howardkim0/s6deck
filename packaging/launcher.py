"""PyInstaller entry point (it needs a script, not a package's __main__)."""
import sys

from s6deck.__main__ import main

sys.exit(main())
