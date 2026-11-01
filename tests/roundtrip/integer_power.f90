! Integer ** integer is an integer: a negative exponent truncates toward zero (1/b**n).
program ipow
  implicit none
  integer :: n, k, m
  n = 2
  k = -2
  m = -3
  print *, 2**(-1), 3**4, (-1)**3, 1**(-5), (-1)**(-4)
  print *, n**k, n**3, m**k, m**3, (-2)**(-3)
  print *, 2.0d0**(-1), 2.0d0**k, 10**2/3
end program ipow
