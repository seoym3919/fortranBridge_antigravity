"""--fflags / --libs of unit_compare and regression_test: defaults unchanged, options reach the gfortran command line."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import unit_compare as uc  # noqa: E402


def test_defaults_are_the_old_build(monkeypatch):
    monkeypatch.delenv("FB_FFLAGS", raising=False)
    monkeypatch.delenv("FB_LIBS", raising=False)
    assert uc.build_options() == {"fflags": ["-O0", "-fwrapv"], "libs": []}


def test_options_from_the_command_line(monkeypatch):
    monkeypatch.delenv("FB_FFLAGS", raising=False)
    monkeypatch.delenv("FB_LIBS", raising=False)
    parser = argparse.ArgumentParser()
    uc.add_build_options(parser)
    args = parser.parse_args(["--fflags", "-g -O3 -falign-loops=16", "--libs", "-llapack -lblas"])
    uc.apply_build_options(args)
    try:
        assert uc.build_options() == {"fflags": ["-g", "-O3", "-falign-loops=16"], "libs": ["-llapack", "-lblas"]}
    finally:
        os.environ.pop("FB_FFLAGS", None)
        os.environ.pop("FB_LIBS", None)


def test_flags_and_libs_reach_gfortran(monkeypatch, tmp_path):
    seen = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        if cmd and cmd[0] == "gfortran":
            seen.append(list(cmd))
        return real(cmd, *a, **k)

    monkeypatch.setattr(uc.subprocess, "run", spy)
    monkeypatch.setenv("FB_FFLAGS", "-O3 -fno-ipa-cp-clone")
    monkeypatch.setenv("FB_LIBS", "-lm")
    src = tmp_path / "s.f90"
    src.write_text("module m\ncontains\nreal function f(x)\nreal x\nf = x\nend function\nend module\n")
    drv = tmp_path / "d.f90"
    drv.write_text("program d\nuse m\nopen(77,file='fb_unit.out',access='stream',form='unformatted')\nwrite(77) f(2.0)\nend program\n")
    work = tmp_path / "w"
    work.mkdir()
    try:
        uc.run_fortran([src], drv, work, b"")
    except uc.UnitError:
        pass                                  # the run may not find its output file here; only the command line matters
    assert seen and "-O3" in seen[0] and "-fno-ipa-cp-clone" in seen[0] and "-O0" not in seen[0] and seen[0].index("-lm") > seen[0].index(str(drv.resolve()))
