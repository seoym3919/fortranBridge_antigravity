! Default INTEGER is 32 bit and wraps modulo 2**32 (reference built with -fwrapv).
program wrap
  implicit none
  integer :: big, seed, i, a, b
  integer(kind=8) :: wide
  big = 2147483647
  big = big + 1
  print *, big
  a = 46341
  b = a*a
  print *, b
  seed = 12345
  do i = 1, 5
    seed = mod(seed*1103515245 + 12345, 2147483647)
  end do
  print *, seed
  wide = 2147483647
  wide = wide + 1
  print *, wide
  print *, -7/2, mod(-7,3), mod(7,-3), 7/(-2), 2147483647/2
  print *, -big
end program wrap
