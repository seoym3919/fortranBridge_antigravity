! READ from standard input (the harness supplies the input): list-directed with repeats, null values and slashes,
! formatted fields, and IOSTAT at the end of the data.
program stdin
  implicit none
  integer i, j, k, ios
  double precision x, y, z
  logical l
  character(len=6) :: s
  read(*,*) i, j
  read(*,*) x, y, z
  read *, k
  read(*,*) l, s
  read(*,'(I3,F6.2,A4)') i, x, s
  read(*,'(2F5.1)') x, y
  read(*,'(F8.3)') z
  read(*,*) i, j, k
  print *, i, j, k
  read(*,*) x
  print *, x
  read(*,*,iostat=ios) i
  print *, ios > 0
  read(*,*,iostat=ios) i
  print *, ios < 0
end program stdin
