! CALL arguments that are array elements with several subscripts.
subroutine put(x, v)
  double precision x, v
  x = v * 2.0d0
end subroutine put

program elems
  implicit none
  double precision a(2,3), b(3)
  integer i, j
  do j = 1, 3
    do i = 1, 2
      a(i,j) = 0.0d0
    end do
    b(j) = 0.0d0
  end do
  call put(a(2,3), 1.5d0)
  call put(a(1, 2), 4.0d0)
  call put(b(2), 7.0d0)
  print *, a(2,3), a(1,2), b(2), a(1,1)
end program elems
