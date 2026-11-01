! Domain and range errors give IEEE results (-inf, +inf, nan), not exceptions or complex numbers.
! (Values come from variables: gfortran rejects constant expressions that overflow.)
program ieee_math
  implicit none
  double precision zero, neg, big, third, thou, two, mtwo, tiny_exp, huge_exp
  real rz
  zero = 0.0d0
  neg = -1.0d0
  big = 1.0d300
  third = 1.0d0 / 3.0d0
  thou = 1000.0d0
  two = 2.0d0
  mtwo = -2.0d0
  tiny_exp = -1075.0d0
  huge_exp = 1030.0d0
  rz = 0.0
  print *, log(zero) < -1.0d300, log(neg) /= log(neg), sqrt(neg) /= sqrt(neg), log10(zero) < -1.0d300
  print *, exp(thou) > 1.0d300, exp(-thou) == 0.0d0, cosh(thou) > 1.0d300, sinh(-thou) < -1.0d300
  print *, acos(two) /= acos(two), asin(mtwo) /= asin(mtwo)
  print *, (-8.0d0)**third /= (-8.0d0)**third, zero**neg > 1.0d300, big**2 > 1.0d300, big*big > 1.0d300
  print *, (-big)**3 < -1.0d300, two**tiny_exp == 0.0d0, two**huge_exp > 1.0d300
  print *, mod(1.0d0, zero) /= mod(1.0d0, zero), sqrt(rz) == 0.0, log(rz) < -1.0e30
end program ieee_math
