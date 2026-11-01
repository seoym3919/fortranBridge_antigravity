! CONTAINS inside a program and inside a subroutine: internal procedures see the host's variables (host association).
subroutine outer(n, result)
  implicit none
  integer n
  double precision result
  integer :: count
  double precision :: total
  count = 0
  total = 0.0d0
  call add(1.5d0)
  call add(2.5d0)
  result = total + scaled(n) + count
contains
  subroutine add(x)
    double precision x
    total = total + x
    count = count + 1
  end subroutine add

  double precision function scaled(k)
    integer k
    scaled = k * total
  end function scaled
end subroutine outer

program intern
  implicit none
  integer, parameter :: m = 4
  double precision :: v(m), s
  integer :: i
  double precision r
  do i = 1, m
    v(i) = dble(i)
  end do
  s = 0.0d0
  call accumulate
  call accumulate
  call outer(3, r)
  print *, s, v, r, twice(s)
contains
  subroutine accumulate
    integer j
    do j = 1, m
      s = s + v(j)
      v(j) = v(j) * 2.0d0
    end do
  end subroutine accumulate

  double precision function twice(x)
    double precision x
    twice = 2.0d0 * x + dble(m)
  end function twice
end program intern
