! Whole-array assignment and expressions, sections, scalar broadcast, elemental intrinsics, reductions, MATMUL/TRANSPOSE.
program wholearr
  implicit none
  integer, parameter :: n = 5
  double precision a(n), b(n), c(n), m(3,3), q(3,3), t(3,3)
  integer iv(n), k
  a = 2.0d0
  do k = 1, n
    b(k) = dble(k)
  end do
  c = a * b + 1.0d0
  print *, c
  c = sqrt(b) + abs(a - 3.0d0 * b)
  print *, c
  b(2:4) = a(1:3) * 10.0d0
  print *, b
  b(2:5) = b(1:4)
  print *, b
  iv = (/ 5, 4, 3, 2, 1 /)
  iv = iv * 2 - 1
  print *, iv, sum(iv), maxval(iv)
  a = b / 2.0d0
  print *, sum(a), sum(a * a), dot_product(a, b)
  do k = 1, 3
    m(k,1) = dble(k)
    m(k,2) = dble(k * k)
    m(k,3) = 1.0d0 / k
  end do
  q = m * 2.0d0
  t = transpose(m)
  print *, q
  print *, t
  m = m + q
  print *, m(2,:)
  print *, m(:,3)
end program wholearr
