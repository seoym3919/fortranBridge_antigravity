! An array element passed to an array dummy argument: the dummy sees the storage from that element onwards (column-major).
subroutine total(x, n, s)
  integer n, i
  double precision x(n), s
  s = 0.0d0
  do i = 1, n
    s = s + x(i)
  end do
end subroutine total

subroutine scale(x, n, f)
  integer n, i
  double precision x(n), f
  do i = 1, n
    x(i) = x(i) * f
  end do
end subroutine scale

program seqassoc
  implicit none
  double precision a(3,2), s, t
  integer i, j
  do j = 1, 2
    do i = 1, 3
      a(i,j) = dble(10*j + i)
    end do
  end do
  call total(a(1,2), 3, s)
  call total(a(2,1), 4, t)
  call scale(a(3,1), 2, 10.0d0)
  print *, s, t, a
end program seqassoc
