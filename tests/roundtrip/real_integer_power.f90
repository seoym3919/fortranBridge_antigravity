! REAL ** INTEGER multiplies by repeated squaring (not libm pow): the last bit depends on the multiplication order.
program rpow
  implicit none
  double precision x, y, z
  real a, b
  integer n, k
  x = 1.0000000123d0
  y = 0.99999987d0
  z = 3.7d0
  a = 1.0000123
  b = 0.9876543
  n = 0
  print *, x**2, x**3, x**4, x**5
  print *, x**6, x**7, x**8, x**9, x**10
  print *, x**11, x**12, x**13, x**15, x**16, x**17
  print *, x**20, x**25, x**31, x**33
  print *, y**2, y**3, y**4, y**5
  print *, y**6, y**7, y**8, y**9, y**10
  print *, y**11, y**12, y**13, y**15, y**16, y**17
  print *, y**20, y**25, y**31, y**33
  print *, z**2, z**3, z**4, z**5
  print *, z**6, z**7, z**8, z**9, z**10
  print *, z**11, z**12, z**13, z**15, z**16, z**17
  print *, z**20, z**25, z**31, z**33
  print *, x**(-2), x**(-3), x**(-5), y**(-4), z**(-7)
  do k = 2, 21
    n = k
    print *, x**n, y**n, z**n, x**(-n)
  end do
  print *, dble(a**2), dble(a**3), dble(a**4), dble(a**5), dble(a**6), dble(a**7), dble(a**9), dble(a**10), dble(a**13)
  print *, dble(b**2), dble(b**3), dble(b**4), dble(b**5), dble(b**6), dble(b**7), dble(b**9), dble(b**10), dble(b**13)
  do k = 2, 12
    n = k
    print *, dble(a**n), dble(b**n), dble(a**(-n))
  end do
end program rpow
