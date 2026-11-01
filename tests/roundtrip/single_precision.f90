! REAL is IEEE single precision: every operation rounds to 24 bits.
program single
  implicit none
  real :: r, s, t, acc, arr(4)
  double precision :: d
  integer :: i
  r = 1.0/3.0
  s = r*3.0
  t = r + 1.0e-8
  d = 0.1d0
  acc = 0.0
  do i = 1, 1000
    acc = acc + 0.1
  end do
  arr(1) = 0.1
  arr(2) = arr(1)*arr(1)
  arr(3) = r*d
  arr(4) = sqrt(2.0)
  print *, dble(r), dble(s), dble(t), dble(acc)
  print *, dble(arr(1)), dble(arr(2)), dble(arr(3)), dble(arr(4))
  print *, dble(real(7)/2), dble(real(1)/3), dble(real(2.5d0*r))
end program single
