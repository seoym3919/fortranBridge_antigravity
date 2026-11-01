! MODULO takes the sign of the divisor, MOD the sign of the dividend.
program modulo_test
  implicit none
  double precision x
  integer i
  x = 7.5d0
  print *, mod(x, 2.0d0), mod(-x, 2.0d0), modulo(x, 2.0d0), modulo(-x, 2.0d0), modulo(x, -2.0d0), modulo(-x, -2.0d0)
  i = 7
  print *, mod(i, 3), mod(-i, 3), mod(i, -3)
  print *, modulo(i, 3), modulo(-i, 3), modulo(i, -3), modulo(-i, -3), modulo(6, 3), modulo(-6, 3)
  ! an INTEGER result stays INTEGER: as a default REAL this would lose its low digits
  i = 2000000011
  print *, modulo(i, 2000000000) + 1999999990, mod(i, 2000000000) + 1999999990
end program modulo_test
