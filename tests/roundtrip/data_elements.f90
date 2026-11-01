! DATA for array elements and implied DO lists; elements not named start at zero (static storage).
program dataelem
  implicit none
  integer a(4), b(3,2), j
  double precision c(3), d(2,2)
  data a(2), a(4) /20, 40/
  data b(2,1), b(1,2) /5, 6/
  data (c(j), j=1,3) /0.5d0, 1.5d0, 2.5d0/
  data d(1,1), d(2,2) /1.0d0, 4.0d0/
  print *, a
  print *, b
  print *, c, d
end program dataelem
