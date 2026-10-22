if __name__ == "__main__":  # (rescoring's worker processes import this module too: they stop here)
    from subsentiment.gui.app import main

    raise SystemExit(main())
