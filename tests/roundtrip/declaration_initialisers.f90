! An initialiser in a declaration (integer :: k = 0) implies SAVE.
subroutine ticks(n)
  integer n
  integer :: count = 0
  double precision :: acc = 0.5d0
  count = count + 1
  acc = acc * 2.0d0
  n = count + int(acc)
end subroutine ticks

program init
  implicit none
  integer n
  call ticks(n)
  call ticks(n)
  call ticks(n)
  print *, n
end program init
