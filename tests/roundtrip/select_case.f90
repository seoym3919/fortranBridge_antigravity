! SELECT CASE with values, lists, ranges (open and closed), CASE DEFAULT, nesting, and EXIT out of the enclosing DO.
integer function classify(k)
  integer k
  select case (k)
  case (:-1)
    classify = -1
  case (0)
    classify = 0
  case (1, 3, 5)
    classify = 1
  case (6:9)
    classify = 2
  case (10:)
    classify = 3
  case default
    classify = 99
  end select
end function classify

program sel
  implicit none
  integer i, total, classify, r
  logical flag
  total = 0
  do i = -2, 12
    total = total + classify(i)
  end do
  print *, total, classify(-5), classify(0), classify(4), classify(5), classify(7), classify(10), classify(100)
  r = 0
  do i = 1, 20
    select case (mod(i, 5))
    case (0)
      if (i > 12) exit
      r = r + 10
    case (1:2)
      select case (i / 5)
      case (0)
        r = r + 1
      case default
        r = r + 100
      end select
    case default
      continue
    end select
  end do
  print *, r, i
  flag = .true.
  select case (flag)
  case (.false.)
    r = -1
  case (.true.)
    r = 1
  end select
  print *, r
end program sel
