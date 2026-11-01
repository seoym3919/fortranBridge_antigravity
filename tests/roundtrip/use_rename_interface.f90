! USE with renaming (local => remote), and an INTERFACE block describing an external procedure.
module physics
  implicit none
  double precision, parameter :: gravity = 9.81d0
  integer :: calls = 0
contains
  double precision function speed(t)
    double precision t
    calls = calls + 1
    speed = gravity * t
  end function speed

  subroutine reset
    calls = 0
  end subroutine reset
end module physics

program userename
  use physics, only: g => gravity, velocity => speed, n_calls => calls, reset_calls => reset
  implicit none
  interface
    double precision function helper(x)
      double precision x
    end function helper
  end interface
  double precision v
  v = velocity(2.0d0) + velocity(1.0d0)
  print *, g, v, n_calls
  call reset_calls
  print *, n_calls, helper(3.0d0)
end program userename

double precision function helper(x)
  double precision x
  helper = x * 2.0d0
end function helper
