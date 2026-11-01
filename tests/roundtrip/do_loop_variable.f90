! The DO variable keeps start + trips*step after the loop; EXIT leaves the current value;
! the trip count is fixed on entry even when the bounds change in the body.
program loops
  implicit none
  integer :: i, j, m, n, total
  m = 3
  do i = 1, m
    m = m + 1
  end do
  print *, i, m
  do j = 5, 1
    total = -1
  end do
  print *, j
  do i = 10, 1, -3
    total = i
  end do
  print *, i, total
  do i = 1, 100
    if (i * i > 50) exit
  end do
  print *, i
  total = 0
  do j = 1, 3
    do i = 1, j
      total = total + i
    end do
  end do
  print *, i, j, total
end program loops
