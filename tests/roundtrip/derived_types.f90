! Derived types: components (scalars, arrays, nested types), arrays of a type, structure constructors, whole-structure
! assignment (a copy), and passing a structure to procedures that change it.
module geom
  implicit none
  type vec
    double precision :: x, y
  end type vec
  type body
    type(vec) :: pos, vel
    double precision :: mass
    integer :: id
    double precision :: hist(3)
  end type body
contains
  subroutine step(b, dt)
    type(body) :: b
    double precision dt
    b%pos%x = b%pos%x + b%vel%x * dt
    b%pos%y = b%pos%y + b%vel%y * dt
    b%hist(1) = b%pos%x
  end subroutine step

  double precision function energy(b)
    type(body) :: b
    energy = 0.5d0 * b%mass * (b%vel%x**2 + b%vel%y**2)
  end function energy
end module geom

program derived
  use geom
  implicit none
  type(body) :: a, c
  type(body) :: fleet(3)
  type(vec) :: v
  integer i
  a%pos = vec(1.0d0, 2.0d0)
  a%vel%x = 0.5d0
  a%vel%y = -0.25d0
  a%mass = 3.0d0
  a%id = 7
  a%hist = 0.0d0
  do i = 1, 3
    fleet(i) = a
    fleet(i)%id = i
    fleet(i)%mass = a%mass * i
    call step(fleet(i), 0.1d0 * i)
  end do
  c = fleet(2)
  c%pos%x = -5.0d0
  v = c%pos
  print *, a%pos%x, a%pos%y, fleet(1)%pos%x, fleet(3)%pos%y
  print *, c%pos%x, fleet(2)%pos%x, v%x, v%y
  print *, energy(fleet(1)), energy(fleet(3)), fleet(2)%id, fleet(3)%hist(1)
end program derived
