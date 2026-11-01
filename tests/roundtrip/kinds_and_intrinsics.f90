! Declared kinds (named, numeric, *N, implicit) decide the precision of every operation.
real function half(x)
  real x
  half = x * 0.5
end function half

double precision function third(x)
  double precision x
  third = x / 3.0
end function third

program kinds
  implicit double precision (a-h, o-z)
  integer, parameter :: dp = kind(1.0d0)
  integer, parameter :: sp = kind(1.0)
  real(dp) :: p, q
  real(sp) :: u
  real*8 :: v
  real(kind=8) :: w
  integer*8 :: big
  integer(8) :: bigger
  real :: h, half
  double precision :: third
  p = 0.1_dp
  q = 1.0_dp / 3.0_dp
  u = 1.0_sp / 3.0_sp
  v = 0.1d0
  w = 0.1
  alpha = 0.1
  big = 2147483647
  big = big * 4
  bigger = 4000000000_8
  h = half(0.3)
  print *, p, q, dble(u), v, w, alpha
  print *, big, bigger, dble(h), third(1.0d0)
  print *, aint(-2.7d0), aint(2.7d0), sign(3, -2), sign(-3, 2), sign(2.5d0, -1.0d0)
  print *, nint(2.5d0), nint(-2.5d0), nint(0.49999999d0), int(-2.7d0), int(2.7)
end program kinds
