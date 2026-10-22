C     ==================================================================
C     LEGACY ASTRONOMICAL ORBIT PROPAGATOR (FORTRAN 77)
C     ==================================================================
      SUBROUTINE PROPAGATE_ORBIT(R0, V0, DT, GM, RF, VF, N_STEPS)
      IMPLICIT DOUBLE PRECISION (A-H, O-Z)
      INTEGER N_STEPS, STEP, I
      DIMENSION R0(3), V0(3), RF(3), VF(3), R_CURR(3), V_CURR(3)
      COMMON /ORBIT_STATE/ ECC, SEMI_MAJOR, INCLINATION, ENERGY
      PARAMETER (TOL = 1.0D-14, SOFTENING = 1.0D-6)

C     INITIALIZE STATE VECTORS (1-BASED INDEXING)
      DO 10 I = 1, 3
         R_CURR(I) = R0(I)
         V_CURR(I) = V0(I)
 10   CONTINUE

C     PROPAGATION LOOP
      DO 100 STEP = 1, N_STEPS
         R_MAG = DSQRT(R_CURR(1)**2 + R_CURR(2)**2 + R_CURR(3)**2 + SOFTENING**2)
         IF (R_MAG .LT. 1.0D-4) GO TO 999

         ACC_FAC = -GM / (R_MAG * R_MAG * R_MAG)

C        DRIFT-KICK INTEGRATION STEP
         DO 50 I = 1, 3
            V_CURR(I) = V_CURR(I) + DT * ACC_FAC * R_CURR(I)
            R_CURR(I) = R_CURR(I) + DT * V_CURR(I)
 50      CONTINUE
 100  CONTINUE

C     COPY FINAL RESULTS
      DO 150 I = 1, 3
         RF(I) = R_CURR(I)
         VF(I) = V_CURR(I)
 150  CONTINUE

C     COMPUTE CONSERVED SPECIFIC ENERGY
      V_SQ = V_CURR(1)**2 + V_CURR(2)**2 + V_CURR(3)**2
      ENERGY = 0.5D0 * V_SQ - GM / R_MAG
      RETURN

 999  PRINT *, 'FATAL: SINGULAR CLOSE ENCOUNTER DETECTED'
      RETURN
      END
