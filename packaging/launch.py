# Entry point for the packaged app (PyInstaller needs a script, not a module).
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()  # rescoring's worker processes start this program again: they stop here
    from subsentiment.gui.app import main

    raise SystemExit(main())
