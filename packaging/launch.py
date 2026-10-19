# Entry point for the packaged app (PyInstaller needs a script, not a module).
from subsentiment.gui.app import main

raise SystemExit(main())
