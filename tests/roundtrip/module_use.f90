! MODULE with parameters, variables with initial values, arrays and contained procedures; USE (whole and ONLY).
module constants
  implicit none
  double precision, parameter :: g = 9.81d0
  double precision, parameter :: two_g = 2.0d0 * g
  integer, parameter :: n = 4
  double precision :: table(n)
  integer :: counter = 0
  real :: scale = 0.5
contains
  double precision function fall(t)
    double precision, intent(in) :: t
    fall = 0.5d0 * g * t * t
  end function fall

  subroutine bump(x)
    double precision x
    counter = counter + 1
    x = x * two_g + counter
  end subroutine bump

  subroutine fill
    integer i
    do i = 1, n
      table(i) = dble(i) * scale
    end do
  end subroutine fill
end module constants

subroutine report(v)
  use constants, only: counter, g
  double precision v
  v = g * counter
end subroutine report

program usem
  use constants
  implicit none
  double precision y, v
  y = 1.0d0
  call bump(y)
  call bump(y)
  call fill
  call report(v)
  print *, fall(2.0d0), y, counter, table(1) + table(4), n, dble(scale), two_g, v
end program usem
