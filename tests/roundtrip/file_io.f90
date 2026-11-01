! OPEN / WRITE / REWIND / READ / CLOSE on a scratch file: list-directed and formatted, IOSTAT, END= and ERR=, implied DO,
! whole arrays, INQUIRE(EXIST=). Self-contained: the program reads back what it writes.
program fileio
  implicit none
  integer i, n, ios, k
  double precision x, y, a(4), b(2,2)
  character(len=8) :: word
  character(len=20) :: fname
  logical ok, flag
  fname = 'fortranbridge_io.tmp'
  open(unit=10, file=fname, status='replace', iostat=ios)
  print *, ios
  write(10,*) 3, 2.5d0, 'abc'
  write(10,'(I4,F8.3,A5)') 42, 3.14159d0, 'hello'
  write(10,*) (i * 1.5d0, i = 1, 4)
  write(10,*) 1.0d0, 2.0d0, 3.0d0, 4.0d0
  write(10,*) .true., 7
  write(10,'(2I3)') 5, 6
  write(10,*) 11
  close(10)
  inquire(file=fname, exist=ok)
  print *, ok
  open(unit=11, file=fname, status='old', iostat=ios)
  read(11,*) n, x, word
  print *, n, x, word
  read(11,'(I4,F8.3,A5)') k, y, word
  print *, k, y, word
  read(11,*) (a(i), i = 1, 4)
  print *, a
  read(11,*) b
  print *, b
  read(11,*) flag, n
  print *, flag, n
  rewind(11)
  read(11,*) k
  print *, k
  do i = 1, 10
    read(11,'(A8)',iostat=ios) word
    if (ios /= 0) exit
  end do
  print *, i, ios < 0
  rewind(11)
  do
    read(11,'(A8)',end=100) word
  end do
100 print *, 'end reached'
  close(11, status='delete')
  inquire(file=fname, exist=ok)
  print *, ok
  open(unit=12, file='does_not_exist.dat', status='old', iostat=ios)
  print *, ios /= 0
end program fileio
