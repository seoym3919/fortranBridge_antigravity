! DATA for array sections, including rows and columns of a matrix (elements fill in array order).
program datasec
  implicit none
  double precision a(5), b(3,3)
  integer c(2,3)
  data a(1:2) /1.5d0, 2.5d0/
  data a(4:5) /4.5d0, 5.5d0/
  data b(2,:) /7.0d0, 8.0d0, 9.0d0/
  data b(1,1:2) /1.0d0, 2.0d0/
  data c(:,2:3) /10, 20, 30, 40/
  print *, a
  print *, b
  print *, c
end program datasec
