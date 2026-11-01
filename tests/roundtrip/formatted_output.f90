! Formatted output: edit descriptors, FORMAT labels, character-variable formats, arrays, implied DO, reversion, complex.
! This fixture is compared as text, character for character.
program fmtout
  implicit none
  integer :: i, k(4)
  double precision :: x, y, z, arr(3)
  real :: r
  complex :: c
  logical :: flag
  character(len=8) :: name
  character(len=12) :: fmt
  x = 3.14159265358979d0
  y = -0.000123456d0
  z = 123456.789d0
  r = 2.5
  k = (/ 1, 22, 333, 4444 /)
  arr = (/ 1.5d0, -2.25d0, 1000.125d0 /)
  flag = .true.
  name = 'Fortran'
  c = (1.5, -2.5)
  i = 42
  write(*,'(I5,F10.3,E14.5)') i, x, y
  write(*,100) x, y, z
100 format(1X,'x=',F8.4,2X,ES12.4,2X,F10.2)
  print '(A,I3)', 'count=', i
  write(*,'(4I6)') k
  write(*,'(3F8.2)') arr
  write(*,'(2(I3,1X),A)') 1, 2, 'end'
  write(*,'(L2,A10,F6.1)') flag, name, r
  write(*,'(I3)') k
  write(*,'(2I4)') k
  write(*,'(F8.3,1X,F8.3)') c
  fmt = '(F9.2,I4)'
  write(*,fmt) z, i
  write(*,'(G12.4,G12.4)') x, z
  write(*,'(D12.4,E12.4E3)') z, z
  write(*,'(F7.2,:,I3)') x
  write(*,'(I3,/,I3,//,I3)') 1, 2, 3
  write(*,'(A,F5.1)') 'val', 1e10
  write(*,'(5X,A)') 'indented'
  write(*,'(F5.2,F5.2)') 0.5, -0.5
  write(*,'(I2,I2,I2)') 1, 2, 3
  write(*,'((A))') 'only'
  write(*,'(10(I2,1X))') (k(i), i=1,4)
  write(*,'(F10.4)') dble(r) / 3.0d0
  write(*,'(F12.5,F12.5)') real(1.0)/3.0, 1.0d0/3.0d0
  write(*,'(ES15.6E3)') 6.02214076d23
  write(*,'(I0,1X,I0)') 7, -12345
  write(*,'(E10.3)') 0.0d0
  write(*,'(F8.2)') -0.001d0
  write(*,'(1X,A,1X,F6.2,1X,A)') 'a', 1.0d0, 'b'
end program fmtout
