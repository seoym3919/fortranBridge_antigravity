! A COMMON block shared between the main program and a subroutine.
subroutine step
  integer n
  double precision x
  common /state/ n, x
  n = n + 1
  x = x * 2.0d0
end subroutine step

program p
  integer n
  double precision x
  common /state/ n, x
  n = 3
  x = 1.5d0
  call step
  call step
  print *, n, x
end program p
