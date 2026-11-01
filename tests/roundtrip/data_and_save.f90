! DATA values are converted to the variable's kind; a DATA or SAVE variable keeps its value between calls;
! arrays fill in column-major order and repeat counts expand (3*1.5d0).
subroutine counter(n)
  integer n, k
  save k
  data k /0/
  k = k + 1
  n = k
end subroutine counter

subroutine accumulate(x, total)
  double precision x, total, running(3)
  integer calls
  data running /1.0d0, 2.0d0, 3.0d0/
  data calls /0/
  calls = calls + 1
  running(1) = running(1) + x
  total = running(1) + running(2) + running(3) + calls
end subroutine accumulate

subroutine tally(m)
  integer m, seen
  real part
  save
  seen = seen + 1
  part = part + 0.1
  m = seen
end subroutine tally

program datatest
  implicit none
  integer n, m, i, tbl(4), grid(2,3)
  double precision t, w(3), z, g(2,2)
  real r
  data tbl /10, 20, 30, 40/
  data w /3*1.5d0/
  data z /0.1/
  data r, i /0.1, -7/
  data grid /1, 2, 3, 4, 5, 6/
  data g /1.0d0, 2.0d0, 3.0d0, 4.0d0/
  call counter(n)
  call counter(n)
  call counter(n)
  call accumulate(1.0d0, t)
  call accumulate(2.0d0, t)
  call tally(m)
  call tally(m)
  print *, n, t, m
  print *, tbl(1) + tbl(4), w(1) + w(2) + w(3), z, dble(r), i
  print *, grid(1,1), grid(2,1), grid(1,3), grid(2,3), g(2,1), g(1,2)
end program datatest
