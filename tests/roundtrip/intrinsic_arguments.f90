! Compound expressions as intrinsic arguments: a translation must keep them whole. `mod(a + 3, b)` written as `a + 3 % b` is a
! different equation, and a wrapped 32-bit value almost never reaches a large modulus, so only arguments like these show it.
program intrinsic_arguments
  implicit none
  integer :: a, b, n
  double precision :: x, y, z
  a = 100
  b = 7
  x = 7.5d0
  y = 2.0d0
  z = 0.25d0
  n = mod(a + 3, b)
  print *, n, mod(a + 3, b) * 2, mod(2 * a - 1, b + 1), mod(a, b) + 1, 2 * mod(a, b - 2), mod(a - b, b - 2) - 1
  print *, modulo(a + 3, b), modulo(-a - 3, b + 1), modulo(a, b - 9), 2 * modulo(a - 1, b)
  ! 5 * modulo(a + 2, b) is 20; written 5 * (a + 2) % b it is 6
  print *, 5 * modulo(a + 2, b), 5.0d0 * modulo(x, y + z), 5 * mod(a + 2, b)
  print *, mod(x + y, y + z), mod(x - z, y * 2.0d0), modulo(x + y, y + z), modulo(-x - y, y - z), 2.0d0 * modulo(x, y - z)
  print *, dim(x, y + z), dim(x - y, z - y), 2.0d0 * dim(x + z, y), idim(a, b + 1), idim(a - b, b + 1), idim(b - a, a)
  print *, sign(x + y, z - y), sign(x - y, y - z), isign(a + 1, b - a), isign(a - 1, a - b)
  print *, max(a + 1, b * 2), min(a - 1, b + 90), max(x - y, y + z, z * 2.0d0), min(x + y, y - z)
  print *, abs(y - x), abs(b - a) + 1, sqrt(x + y) * 2.0d0, exp(y - z) / 2.0d0
end program intrinsic_arguments
