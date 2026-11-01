import shutil
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import unit_compare as uc

pytestmark = pytest.mark.skipif(shutil.which("gfortran") is None, reason="needs gfortran")
DRIVER = """program d
   character(len=512) :: path
   call get_environment_variable('FB_UNIT_OUT', path)
   open (77, file=trim(path), access='stream', form='unformatted')
   write (77) 7
   close (77)
   stop 1
end program d
"""


def _build(tmp_path):
    source = tmp_path / "lib.f90"
    source.write_text("subroutine unused()\nend subroutine unused\n")
    driver = tmp_path / "d.f90"
    driver.write_text(DRIVER)
    return [source], driver


def test_expected_exit_accepts_the_stop(tmp_path):
    sources, driver = _build(tmp_path)
    (tmp_path / "w").mkdir()
    assert uc.run_fortran(sources, driver, tmp_path / "w", b"", expected_exit=1) == (7).to_bytes(4, "little")


def test_default_still_refuses_a_nonzero_exit(tmp_path):
    sources, driver = _build(tmp_path)
    (tmp_path / "w").mkdir()
    with pytest.raises(uc.UnitError):
        uc.run_fortran(sources, driver, tmp_path / "w", b"")
