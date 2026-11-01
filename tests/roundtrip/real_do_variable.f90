! A REAL DO variable: the trip count is max(int((end - start + step) / step), 0) in the variable's precision,
! the variable accumulates x = x + step, and ends one step past the last iteration.
program realdo
  implicit none
  real x
  double precision y
  integer n
  n = 0
  do x = 0.0, 1.0, 0.1
    n = n + 1
  end do
  print *, n, dble(x)
  n = 0
  do y = 1.0d0, 2.0d0, 0.3d0
    n = n + 1
  end do
  print *, n, y
  n = 0
  do y = 5.0d0, 1.0d0
    n = -1
  end do
  print *, n, y
  do y = 2.0d0, 0.0d0, -0.5d0
    n = n + 100
  end do
  print *, n, y
  do y = 0.0d0, 1.0d0, 0.25d0
    if (y < 0.3d0) cycle
    n = n + 1
    if (y > 0.6d0) exit
  end do
  print *, n, y
end program realdo
