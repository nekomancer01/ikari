"""What usually breaks the packaged app but not the tests run from source, checked without building it.

Pull requests don't run PyInstaller (main and release tags do), so these catch the build breaks that can be
seen from the source: a data file the bundle would leave out, a Qt module the spec strips, an import
PyInstaller can't follow, or a file the spec or installer points at that isn't there.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "subsentiment"
SPEC = ROOT / "packaging" / "subsentiment.spec"
SPEC_TEXT = SPEC.read_text(encoding="utf-8")


def _spec_list(name: str) -> list[str]:
    """A list literal passed to Analysis(...) in the spec, e.g. excludes."""
    for node in ast.walk(ast.parse(SPEC_TEXT)):
        if isinstance(node, ast.keyword) and node.arg == name and isinstance(node.value, ast.List):
            return [ast.literal_eval(e) for e in node.value.elts]
    raise AssertionError(f"{name}=[...] not found in {SPEC.name}")


def _app_sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)  # from PySide6 import QtMultimedia
    return names


def _bundled_files(package: str) -> set[str]:
    from PyInstaller.utils.hooks import collect_data_files  # what the spec bundles

    return {Path(src).resolve().as_posix() for src, _dest in collect_data_files(package)}


def test_every_data_file_in_the_app_goes_into_the_bundle():
    data = {p.resolve().as_posix() for p in SRC.rglob("*")
            if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts}
    assert data, "no data files found; has the package moved?"
    missing = sorted(data - _bundled_files("subsentiment"))
    assert not missing, f"the build would leave these out: {missing}"


def test_every_data_file_is_in_the_package_data():
    # Wheels and non-editable installs only carry files matched by [tool.setuptools.package-data].
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("[tool.setuptools.package-data]", 1)[1].split("\n[", 1)[0]
    globs = {pkg: ast.literal_eval(pats) for pkg, pats in re.findall(r'"([\w.]+)"\s*=\s*(\[[^\]]*\])', block)}
    for p in SRC.rglob("*"):
        if not p.is_file() or p.suffix in (".py", ".pyc") or "__pycache__" in p.parts:
            continue
        matched = False
        for pkg, patterns in globs.items():
            pkg_dir = SRC.parent.joinpath(*pkg.split("."))
            if p.is_relative_to(pkg_dir):
                matched |= any(p.relative_to(pkg_dir).match(g) for g in patterns)
        assert matched, f"{p.relative_to(ROOT)} isn't covered by pyproject's package-data"


def test_vader_lexicon_goes_into_the_bundle():
    assert any(f.endswith("vader_lexicon.txt") for f in _bundled_files("vaderSentiment"))


@pytest.mark.parametrize("path", _app_sources(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_module_the_spec_strips_out_is_imported(path):
    excluded = _spec_list("excludes")
    used = {name for name in _imports(path) for ex in excluded if name == ex or name.startswith(ex + ".")}
    assert not used, f"{path.name} imports {sorted(used)}, which the spec excludes from the build"


@pytest.mark.parametrize("path", _app_sources(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_import_is_hidden_from_pyinstaller(path):
    # PyInstaller follows import statements only; a module loaded by name has to be in hiddenimports.
    hidden = {name for lst in re.findall(r"hiddenimports \+?= (\[[^\]]*\])", SPEC_TEXT)
              for name in ast.literal_eval(lst)}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in ("import_module", "__import__"):
                arg = node.args[0] if node.args else None
                target = arg.value if isinstance(arg, ast.Constant) else None
                assert target in hidden, (
                    f"{path.name}:{node.lineno} loads a module by name ({ast.unparse(node)}); "
                    "add it to hiddenimports in packaging/subsentiment.spec")


def test_files_the_spec_and_installer_point_at_exist():
    assets = SRC / "gui" / "assets"
    for name in re.findall(r'os\.path\.join\(ASSETS, "([^"]+)"\)', SPEC_TEXT):
        assert (assets / name).is_file(), f"the spec needs gui/assets/{name}"
    assert (ROOT / "packaging" / "launch.py").is_file()
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    icon = re.search(r"SetupIconFile=(.+)", iss).group(1).strip().replace("\\", "/")
    assert (ROOT / "packaging" / icon).resolve().is_file(), f"the installer needs {icon}"


def test_the_entry_point_and_version_the_build_reads():
    launch = (ROOT / "packaging" / "launch.py").read_text(encoding="utf-8")
    module, func = re.search(r"from ([\w.]+) import (\w+)", launch).groups()
    mod = __import__(module, fromlist=[func])
    assert callable(getattr(mod, func))
    init = (SRC / "__init__.py").read_text(encoding="utf-8")
    assert re.search(r'__version__ = "(\d+(\.\d+)*)"', init), "the spec and build.py read __version__ this way"
