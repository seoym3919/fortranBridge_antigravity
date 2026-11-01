! MATMUL products: matrix-vector, vector-matrix and matrix-matrix, single and double precision.
! libgfortran's kernel is blocked and may use FMA, so this fixture is compared with an explicit ULP bound (see test_roundtrip).
program mv
  implicit none
  integer, parameter :: n = 7
  double precision a(n,n), b(n,n), c(n,n), v(n), w(n), u(n)
  real r(n,n), s(n), t(n)
  integer i, j
  do j = 1, n
    do i = 1, n
      a(i,j) = 1.0d0 / (i + 2.0d0*j)
      r(i,j) = 1.0 / (i + 2.0*j)
      b(i,j) = sin(dble(i*j)) + 0.1d0
    end do
    v(j) = 1.0d0 / (j + 0.3d0)
    s(j) = 1.0 / (j + 0.3)
  end do
  w = matmul(a, v)
  u = matmul(v, a)
  t = matmul(r, s)
  c = matmul(a, b)
  print *, w
  print *, u
  do i = 1, n
    print *, dble(t(i))
  end do
  print *, c
end program mv
