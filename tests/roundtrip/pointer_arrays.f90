! POINTER arrays: p => a (an alias: writes through p change a), ASSOCIATED, NULLIFY, ALLOCATE of a pointer array.
program ptr
  implicit none
  double precision, target :: a(5), b(3)
  double precision, pointer :: p(:), q(:)
  integer i
  do i = 1, 5
    a(i) = dble(i)
  end do
  b = 10.0d0
  p => a
  p(2) = 99.0d0
  print *, a(2), p(2), size(p), associated(p)
  q => b
  q(1) = q(1) + p(5)
  print *, b, associated(q)
  nullify(p)
  print *, associated(p)
  allocate(p(4))
  p = 3.5d0
  p(4) = -1.0d0
  print *, sum(p), size(p), associated(p)
end program ptr
