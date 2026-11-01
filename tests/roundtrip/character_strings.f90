! CHARACTER(n): assignment pads with blanks or truncates; comparison ignores trailing blanks; // concatenates; TRIM / LEN_TRIM.
program chars
  implicit none
  character(len=8) :: a, b
  character(len=4) :: short
  character(len=20) :: both
  integer :: n
  a = 'Fortran'
  b = 'Fortran '
  short = 'abcdefgh'
  both = a // '-' // short
  n = len_trim(a)
  print *, a == b, a /= b, a == 'Fortran', a < 'Gortran', short == 'abcd', n, len(a), len(both)
  print *, trim(a) // '|', both
  write(*,'(A10,A,A)') a, '|', short
  write(*,'(2A)') trim(a), short
  if (a == b) then
    print *, 'equal'
  end if
end program chars
