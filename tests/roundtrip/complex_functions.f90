! Elemental functions of a complex argument call libm's csqrt / cexp / clog.
! (Python-only in test_roundtrip: a C++ standard library may implement them itself -- libc++ does -- instead of calling libm.)
program cfun
  implicit none
  complex(kind=8) dz, ds
  dz = (1.0d0, 2.0d0)
  ds = sqrt(dz) + exp(dz)
  print *, real(ds), aimag(ds)
  ds = log(dz) + sin(dz) * cos(dz)
  print *, real(ds), aimag(ds)
end program cfun
