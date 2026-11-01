! COMPLEX: default kind is single precision (COMPLEX = two REALs); literals, mixed kinds, conjg/cmplx/aimag/abs, elemental functions.
program cplx
  implicit none
  complex z, w, u
  complex(kind=8) dz, dw, ds
  double precision x
  real r
  integer i
  z = (1.5, -2.0)
  w = (0.25, 3.0)
  u = z * w + z / w - w
  dz = (1.0d0, 2.0d0)
  dw = dz * dz + conjg(dz) / (3.0d0, 4.0d0)
  x = abs(dz)
  r = abs(z)
  print *, dble(real(u)), dble(aimag(u)), dble(r)
  print *, real(dw), aimag(dw), x
  ds = dcmplx(0.5d0, -0.25d0) * 2.0d0 + dz
  print *, real(ds), aimag(ds)
  z = cmplx(r, 2.0)
  print *, dble(real(z)), dble(aimag(z)), z == w, z /= w
  u = (0.0, 0.0)
  do i = 1, 5
    u = u + z * w / cmplx(real(i), 1.0)
  end do
  print *, dble(real(u)), dble(aimag(u))
  dw = dz ** 2 + dz ** 3
  print *, real(dw), aimag(dw)
  ds = dz + z
  print *, real(ds), aimag(ds)
  ! more divisions, both kinds, both Smith branches (operands come from variables: gfortran folds constant expressions
  ! with exact arithmetic at compile time, which no runtime algorithm reproduces)
  z = (3.7, -1.3)
  w = (0.9, 2.2)
  u = z / w
  z = (-0.31, 5.5)
  w = (7.1, -0.4)
  r = real(z / w)
  print *, dble(real(u)), dble(aimag(u)), dble(r), dble(aimag(z / w))
  dz = (3.7d0, -1.3d0)
  dw = (0.9d0, 2.2d0)
  ds = dz / dw
  print *, real(ds), aimag(ds)
  dz = (-0.31d0, 5.5d0)
  dw = (7.1d0, -0.4d0)
  ds = dz / dw
  print *, real(ds), aimag(ds), real(dz / (0.1d0, 0.2d0)), aimag(dz / (0.1d0, 0.2d0))
end program cplx
