! SUM / PRODUCT / DOT_PRODUCT accumulate in array order, in the precision of the array.
program reductions
  implicit none
  integer, parameter :: n = 5000
  double precision a(n), c(5), s, d, pr
  real b(n), sb, db
  integer k(6), ks, i
  do i = 1, n
    a(i) = 1.0d0 / i
    b(i) = 1.0 / i
  end do
  do i = 1, 6
    k(i) = i * 1000
  end do
  s = sum(a)
  d = dot_product(a, a)
  do i = 1, 5
    c(i) = 1.0d0 / (i + 0.5d0)
  end do
  pr = product(c)
  sb = sum(b)
  db = dot_product(b, b)
  ks = sum(k)
  print *, s, d, pr
  print *, dble(sb), dble(db), ks
end program reductions
