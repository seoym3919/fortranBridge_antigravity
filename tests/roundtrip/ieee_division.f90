! Real division by zero is IEEE infinity / NaN, not an exception.
program ieee
  implicit none
  double precision x, y, z, nz
  real a, b
  x = 1.0d0
  y = 0.0d0
  nz = -0.0d0
  z = x / y
  print *, z > 1.0d300, (-x)/y < -1.0d300, x/nz < -1.0d300
  z = y / y
  print *, z /= z, z == z
  a = 1.0
  b = 0.0
  print *, a/b > 1.0e30, (a - a)/b /= (a - a)/b
end program ieee
