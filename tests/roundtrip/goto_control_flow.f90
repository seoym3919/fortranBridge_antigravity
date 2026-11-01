! GO TO, computed GO TO and arithmetic IF: backward loops, forward skips, jumps out of DO loops and IF blocks.
program gotos
  implicit none
  integer i, s, k, n, total
  double precision x
  i = 0
  s = 0
10 i = i + 1
  s = s + i
  if (i .lt. 5) goto 10
  print *, s
  total = 0
  do k = -1, 1
    if (k) 20, 30, 40
20  total = total - 1
    goto 50
30  total = total + 10
    goto 50
40  total = total + 100
50  continue
  end do
  print *, total
  n = 0
  do i = 1, 10
    do k = 1, 10
      if (i * k .gt. 20) goto 60
      n = n + 1
    end do
  end do
60 print *, n, i, k
  do i = 1, 3
    goto (70, 80, 90), i
70  total = 1
    goto 95
80  total = 2
    goto 95
90  total = 3
95  print *, i, total
  end do
  x = 1.0d0
100 x = x * 1.5d0
  if (x < 10.0d0) goto 100
  print *, x
  i = 0
  do while (i < 100)
    i = i + 7
    if (i > 30) goto 110
  end do
110 print *, i
  if (s > 10) then
    if (total == 3) goto 120
    s = -1
  end if
  s = s + 1000
120 print *, s
end program gotos
