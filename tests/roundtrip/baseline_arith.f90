! Baseline: constructs that already translate correctly; guards against regressions.
double precision function poly(x)
  double precision x
  poly = 3.0d0*x*x - 2.0d0*x + 1.0d0
end function poly

subroutine axpy(n, a, x, y)
  integer n, i
  double precision a, x(n), y(n)
  do i = 1, n
    y(i) = y(i) + a*x(i)
  end do
end subroutine axpy

program baseline
  implicit none
  integer, parameter :: n = 5
  integer :: i, k
  double precision :: x(n), y(n), s, poly
  do i = 1, n
    x(i) = dble(i) * 0.25d0
    y(i) = 1.0d0
  end do
  call axpy(n, 2.0d0, x, y)
  s = 0.0d0
  do i = 1, n
    s = s + y(i)
  end do
  print *, s, poly(1.5d0), y(1), y(n)
  k = 7
  print *, k / 2, mod(k, 3), abs(-k), max(k, 9), min(k, 2)
  print *, sqrt(2.0d0), exp(0.0d0), 2.0d0**3, 2.0d0**(-2)
end program baseline
