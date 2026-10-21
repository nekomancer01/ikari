# As packaging/launch.py, then sets a word's meaning in a big subreddit through the real window and reports
# whether worker processes scored it: a check of the packaged app's rescoring, not shipped.
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    import os, sys, time
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QSettings, QTimer
    from PySide6.QtWidgets import QApplication
    from subsentiment.gui import words
    from subsentiment.gui.main_window import MainWindow
    db, sub, word = sys.argv[1:4]
    app = QApplication(sys.argv[:1]); app.setOrganizationName("ikari-frozen-check"); app.setApplicationName(str(os.getpid()))
    QSettings().setValue("updates/auto", False)
    w = MainWindow(db); w.show()
    w._show_dataset(sub, wait=True)
    m = w.words_page.meanings
    m.pick(word); m.rows[word].set(0.0)
    pooled = []
    w.rescore.pool.scored.connect(lambda tag, r: pooled.append(len(r)))
    loop = QEventLoop(); w.rescore.finished.connect(lambda s: loop.quit())
    QTimer.singleShot(600_000, loop.quit)
    t = time.perf_counter()
    m._save(m.rows[word])
    loop.exec()
    print(f"frozen={getattr(sys, 'frozen', False)} took {time.perf_counter() - t:.1f}s; chunks from processes: {len(pooled)} "
          f"({sum(pooled)} items); broken={w.rescore.pool.broken}; windows open: {len(QApplication.topLevelWidgets())}; "
          f"{m.rows[word].result.full_text()}", flush=True)
    w.close()
    raise SystemExit(0 if pooled and not w.rescore.pool.broken else 1)
