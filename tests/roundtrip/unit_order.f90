! The main program comes first and calls units defined after it.
program order
  implicit none
  double precision v, scale2
  integer k, nsteps
  v = 3.0d0
  call twice(v)
  k = nsteps(10)
  print *, v, scale2(v), k
end program order

subroutine twice(x)
  double precision x
  x = 2.0d0 * x
end subroutine twice

double precision function scale2(x)
  double precision x
  scale2 = x * x
end function scale2

integer function nsteps(n)
  integer n
  nsteps = n / 3
end function nsteps
