! Array constructors (/ /) and [ ], implied DO in constructors and in output lists, constructors in expressions.
program ctor
  implicit none
  integer v(5), w(3), i, n, tab(3)
  double precision a(4), s
  real r(3)
  integer :: init(3) = (/ 7, 8, 9 /)
  n = 4
  v = (/ 1, 2, 3, 4, 5 /)
  w = [10, 20, 30]
  a = (/ (dble(i)*0.5d0, i=1,n) /)
  r = (/ 0.1, 0.2, 0.3 /)
  tab = (/ 3, (i*i, i=1,2) /)
  s = sum((/ 1.5d0, 2.5d0, 3.5d0 /))
  print *, v, w
  print *, a
  print *, (dble(r(i)), i=1,3)
  print *, (v(i)*2, i=1,5,2), s
  v = v * 2 + (/ 5, 4, 3, 2, 1 /)
  print *, v, maxval(v), sum(v)
  print *, tab, init, sum(a * 2.0d0)
end program ctor
