! Units may name the members of one COMMON block differently: association is by position, not by name.
! Members left undeclared take implicit types (I-N integer, others single precision REAL).
subroutine bump
  integer count
  double precision total
  common /acc/ count, total, scale
  count = count + 1
  total = total + 0.5d0 * count
  scale = scale * 2.0
end subroutine bump

program p
  integer n
  double precision s
  common /acc/ n, s, factor
  n = 0
  s = 0.0d0
  factor = 0.1
  call bump
  call bump
  call bump
  print *, n, s, dble(factor)
end program p
