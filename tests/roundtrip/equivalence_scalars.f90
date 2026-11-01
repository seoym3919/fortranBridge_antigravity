! EQUIVALENCE of scalars: two names for one variable.
program eqscalar
  implicit none
  double precision x, y
  integer m, n
  equivalence (x, y), (m, n)
  x = 1.5d0
  y = y * 2.0d0
  m = 7
  n = n + 1
  print *, x, y, m, n
end program eqscalar
