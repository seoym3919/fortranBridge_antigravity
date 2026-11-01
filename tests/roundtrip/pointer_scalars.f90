! POINTER to scalars and derived-type objects: p => x aliases x (writes through p change x), pointer to pointer, ALLOCATE of a pointer,
! NULLIFY, ASSOCIATED.
program ptrs
  implicit none
  type node
    double precision :: val
    integer :: id
  end type node
  double precision, target :: x, y
  integer, target :: n
  double precision, pointer :: p, q
  integer, pointer :: ip
  type(node), target :: a
  type(node), pointer :: np, other
  x = 1.5d0
  y = 2.5d0
  n = 7
  p => x
  p = p + 10.0d0
  q => p
  q = q * 2.0d0
  ip => n
  ip = ip + 1
  print *, x, y, n, associated(p), associated(q)
  p => y
  print *, p, x
  a%val = 3.0d0
  a%id = 4
  np => a
  np%val = np%val * 2.0d0
  np%id = np%id + 1
  print *, a%val, a%id, associated(np)
  allocate(other)
  other%val = -1.0d0
  other%id = 9
  np => other
  print *, np%val, np%id, a%val
  nullify(np)
  print *, associated(np)
  deallocate(other)
end program ptrs
