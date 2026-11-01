! ALLOCATABLE arrays: ALLOCATE with bounds (including a non-unit lower bound), DEALLOCATE, ALLOCATED, SIZE, and assumed-shape
! dummy arguments x(:).
module work
  implicit none
contains
  subroutine fillit(x)
    double precision, intent(inout) :: x(:)
    integer i
    do i = 1, size(x)
      x(i) = x(i) + dble(i)
    end do
  end subroutine fillit

  double precision function total(m)
    double precision, intent(in) :: m(:,:)
    integer i, j
    total = 0.0d0
    do j = 1, size(m, 2)
      do i = 1, size(m, 1)
        total = total + m(i, j)
      end do
    end do
  end function total
end module work

program al
  use work
  implicit none
  integer n, i
  double precision, allocatable :: a(:), m(:,:)
  integer, allocatable :: idx(:)
  n = 4
  allocate(a(n), m(2,3), idx(0:2))
  a = 1.0d0
  call fillit(a)
  do i = 0, 2
    idx(i) = i * 10
  end do
  m = 2.0d0
  m(2,3) = 7.5d0
  print *, a, total(m), idx, size(a), size(m, 1), size(m, 2), allocated(a)
  deallocate(a)
  print *, allocated(a), allocated(m)
  allocate(a(2))
  a(1) = 5.0d0
  a(2) = 6.0d0
  print *, a, allocated(a)
end program al
