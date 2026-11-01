! FLOOR / CEILING return integers; EPSILON / HUGE / TINY depend on the kind of their argument.
program intr
  implicit none
  double precision x
  real r
  x = -2.5d0
  r = 7.9
  print *, floor(x), ceiling(x), floor(2.0d0), ceiling(2.1d0), floor(r), ceiling(r)
  print *, epsilon(x), dble(epsilon(r)), tiny(x), huge(x)
  print *, dble(huge(r)), dble(tiny(r)), huge(1), huge(x) > 1.0d300
end program intr
