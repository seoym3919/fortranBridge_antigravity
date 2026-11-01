! A REAL (single precision) literal assigned to a DOUBLE PRECISION variable keeps only single precision.
program lit
  implicit none
  double precision :: x, y, z, w
  x = 0.1
  y = 0.1d0
  z = x + 0.5
  w = 1.0e-1
  print *, x, y, z, w
  print *, x == y
end program lit
