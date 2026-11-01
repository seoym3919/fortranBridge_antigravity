! EQUIVALENCE of arrays and array elements: overlapping storage (Python; C++ fails loudly, std::vector storage cannot overlap).
program eqarray
  implicit none
  double precision a(4), b(2), c(2,2), d(4)
  double precision u, v(3)
  integer i
  equivalence (a(3), b(1)), (c, d), (u, v(2))
  do i = 1, 4
    a(i) = dble(i)
    d(i) = dble(10 * i)
  end do
  b(2) = 99.0d0
  u = -1.0d0
  print *, a, b, c, v(2)
  c(1,2) = 5.0d0
  print *, d, u
end program eqarray
