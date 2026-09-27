"""Tests for the installed ``prisma-vent`` command.

The exit codes are a contract, so they are asserted rather than observed.
"""

import io
from datetime import date, datetime

import pytest

from prisma_vent.cli import (
    EXIT_IO,
    EXIT_OK,
    EXIT_SAFETY,
    EXIT_STRUCTURAL,
    EXIT_USAGE,
    main,
)
from synthetic import build_day_archive, build_trend_curve

ARCHIVE_DATE = date(2020, 3, 4)


def run(*args):
    out = io.StringIO()
    return main([str(a) for a in args], out=out), out.getvalue()


@pytest.fixture
def archive(tmp_path):
    return build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 120)],
    )


# --------------------------------------------------------------------------
# The command exists and dispatches
# --------------------------------------------------------------------------


def test_version_is_reported():
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == EXIT_OK


def test_inspect_subcommand_runs(archive):
    status, text = run("inspect", archive)
    assert status == EXIT_OK
    assert archive.name in text


def test_validate_subcommand_runs(archive):
    status, text = run("validate", "--report", archive)
    assert status == EXIT_OK
    assert "sessions read:" in text


def test_inspect_accepts_a_trend_curve(tmp_path):
    path = build_trend_curve(tmp_path, day=date(2020, 1, 1), populated=5)
    status, text = run("inspect", path)
    assert status == EXIT_OK
    assert "therapy day     2020-01-01" in text


def test_subcommand_options_are_the_module_s_own(archive):
    """The installed command and the module must not describe different tools."""
    status, text = run("inspect", "--no-stats", archive)
    assert status == EXIT_OK
    assert "observed (physical)" not in text


# --------------------------------------------------------------------------
# Exit codes are a contract
# --------------------------------------------------------------------------


def test_exit_codes_are_distinct():
    codes = {EXIT_OK, EXIT_STRUCTURAL, EXIT_USAGE, EXIT_IO, EXIT_SAFETY}
    assert len(codes) == 5


def test_no_subcommand_is_a_usage_error(capsys):
    assert main([]) == EXIT_USAGE
    # Help goes to stderr: a caller who invoked this wrongly asked for no
    # output, and stdout must stay usable in a pipeline.
    assert "usage: prisma-vent" in capsys.readouterr().err


def test_unknown_subcommand_is_a_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        main(["nonsense"])
    assert excinfo.value.code == EXIT_USAGE


def test_bad_option_value_is_a_usage_error(archive):
    with pytest.raises(SystemExit) as excinfo:
        run("inspect", "--samples", "-1", archive)
    assert excinfo.value.code == EXIT_USAGE


def test_unreadable_file_is_a_structural_failure(tmp_path, capsys):
    bad = tmp_path / "0123_2020-03-04.zip"
    bad.write_bytes(b"not a zip")
    status, text = run("inspect", bad)
    assert status == EXIT_STRUCTURAL
    assert text == ""  # nothing on stdout
    assert "not a readable ZIP" in capsys.readouterr().err


def test_missing_file_is_a_structural_failure(tmp_path, capsys):
    status, _ = run("inspect", tmp_path / "0123_2020-03-04.zip")
    assert status == EXIT_STRUCTURAL
    assert "no such file" in capsys.readouterr().err


# --------------------------------------------------------------------------
# stdout and stderr stay separate
# --------------------------------------------------------------------------


def test_findings_go_to_stdout_and_diagnostics_to_stderr(tmp_path, capsys):
    good = build_day_archive(tmp_path, archive_date=ARCHIVE_DATE)
    bad = tmp_path / "0479_2020-03-05.zip"
    bad.write_bytes(b"not a zip")

    status, text = run("inspect", bad, good)
    captured = capsys.readouterr()

    assert status == EXIT_STRUCTURAL
    assert good.name in text  # the readable one still reported on stdout
    assert good.name not in captured.err
    assert "not a readable ZIP" in captured.err


def test_only_implemented_commands_are_registered():
    """The command must not advertise what has not been written.

    Checked against the registered names rather than the help text, where
    "decode" also occurs inside "decoded archives" and a substring search
    would pass or fail for the wrong reason.
    """
    from prisma_vent.cli import _COMMANDS

    assert set(_COMMANDS) == {
        "copy-card",
        "decode",
        "inspect",
        "usage",
        "validate",
    }


def test_the_version_matches_pyproject():
    """Two declarations of one version, so something has to compare them.

    `prisma_vent.__version__` is what `--version` prints and what lands in
    `adapter.package_version` in every export and copy manifest; the version in
    pyproject.toml is what names the built artefact. The 0.1.0 release was
    almost cut with the two disagreeing — a wheel called 0.1.0 whose exports
    recorded 0.1.0.dev0 — which for a decoder whose claim is traceable
    provenance is not a cosmetic difference.
    """
    import re
    import tomllib
    from pathlib import Path

    from prisma_vent import __version__

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():          # installed wheel, no source tree beside it
        import pytest

        pytest.skip("no pyproject.toml beside the package")

    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert __version__ == declared, (
        f"prisma_vent.__version__ is {__version__!r} but pyproject.toml says "
        f"{declared!r}"
    )
    assert re.match(r"^\d+\.\d+\.\d+", __version__)
