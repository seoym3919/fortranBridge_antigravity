r"""MATMUL for translated Python modules (embedded verbatim; see fortran_to_python._HELPERS).

libgfortran's MATMUL is a blocked kernel compiled with -ffast-math and vectorised; which variant runs (AVX512, AVX2+FMA,
AVX, generic) depends on the CPU, and the order of the sums inside it cannot be reproduced by any fixed formula. So for real
arrays this calls libgfortran's own routine (`_gfortran_matmul_r8` / `_r4`) through ctypes, which gives gfortran's result
bit for bit on the same machine. Without libgfortran (or for other types) it sums over the shared index in order, in the
arrays' precision; that agrees to a few ULP, not bitwise. Set FORTRANBRIDGE_NO_LIBGFORTRAN=1 to force the fallback.
Needs the imports ctypes, ctypes.util, os, numpy as np.
"""


class _FDim(ctypes.Structure):
    _fields_ = [("stride", ctypes.c_ssize_t), ("lbound", ctypes.c_ssize_t), ("ubound", ctypes.c_ssize_t)]


class _FDType(ctypes.Structure):
    _fields_ = [("elem_len", ctypes.c_size_t), ("version", ctypes.c_int), ("rank", ctypes.c_byte),
                ("type", ctypes.c_byte), ("attribute", ctypes.c_short)]


_flib_cache = []


def _flib():
    """The loaded libgfortran, or None."""
    if not _flib_cache:
        lib = None
        if not os.environ.get("FORTRANBRIDGE_NO_LIBGFORTRAN"):
            names = ["libgfortran.so.5", "libgfortran.so.4", "libgfortran.so.3", ctypes.util.find_library("gfortran")]
            for name in names:
                if not name:
                    continue
                try:
                    candidate = ctypes.CDLL(name)
                    if hasattr(candidate, "_gfortran_matmul_r8"):
                        lib = candidate
                        break
                except OSError:
                    continue
        _flib_cache.append(lib)
    return _flib_cache[0]


def _fdescriptor(arr):
    class Descriptor(ctypes.Structure):
        _fields_ = [("base_addr", ctypes.c_void_p), ("offset", ctypes.c_size_t), ("dtype", _FDType),
                    ("span", ctypes.c_ssize_t), ("dim", _FDim * arr.ndim)]

    d = Descriptor()
    d.base_addr, d.offset = arr.ctypes.data, 0
    d.dtype.elem_len, d.dtype.version, d.dtype.rank, d.dtype.type, d.dtype.attribute = arr.itemsize, 0, arr.ndim, 3, 0
    d.span = arr.itemsize
    stride = 1
    for k in range(arr.ndim):
        d.dim[k].stride, d.dim[k].lbound, d.dim[k].ubound = stride, 0, arr.shape[k] - 1
        stride *= arr.shape[k]
    return d


def _fmatmul_lib(a, b):
    lib = _flib()
    if lib is None or a.dtype != b.dtype or a.dtype not in (np.float64, np.float32) or a.ndim not in (1, 2) \
            or b.ndim not in (1, 2) or (a.ndim == 1 and b.ndim == 1):
        return None
    a, b = np.asfortranarray(a), np.asfortranarray(b)
    shape = (a.shape[0], b.shape[1]) if a.ndim == 2 and b.ndim == 2 else ((a.shape[0],) if a.ndim == 2 else (b.shape[1],))
    c = np.zeros(shape, dtype=a.dtype, order="F")
    fn = lib._gfortran_matmul_r8 if a.dtype == np.float64 else lib._gfortran_matmul_r4
    fn.argtypes = [ctypes.c_void_p] * 3 + [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    da, db, dc = _fdescriptor(a), _fdescriptor(b), _fdescriptor(c)
    fn(ctypes.byref(dc), ctypes.byref(da), ctypes.byref(db), 0, 30, None)
    return c


def _fmatmul(a, b):
    """MATMUL of two arrays (matrix-matrix, matrix-vector or vector-matrix)."""
    a, b = np.asarray(a), np.asarray(b)
    c = _fmatmul_lib(a, b)
    if c is not None:
        return c
    dtype = np.result_type(a, b)
    if a.ndim == 2 and b.ndim == 2:
        c = np.zeros((a.shape[0], b.shape[1]), dtype=dtype, order="F")
        for k in range(a.shape[1]):
            c = c + np.outer(a[:, k], b[k, :])
        return c
    if a.ndim == 2:
        c = np.zeros(a.shape[0], dtype=dtype)
        for k in range(a.shape[1]):
            c = c + a[:, k] * b[k]
        return c
    c = np.zeros(b.shape[1], dtype=dtype)
    for k in range(a.shape[0]):
        c = c + a[k] * b[k, :]
    return c
