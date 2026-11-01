// MATMUL for translated C++ files (embedded verbatim; see fortran_to_cpp._MATMUL_RUNTIME). Mirrors fortran_matmul_runtime.py.
// libgfortran's kernel is compiled with -ffast-math and vectorised, and which variant runs depends on the CPU, so no fixed sum
// reproduces it. For float and double this loads libgfortran at run time and calls its own routine, which gives gfortran's
// result bit for bit on the same machine. Without the library (or for other types) it sums over the shared index in order; that
// differs from gfortran by up to 18 ULP (3.3e-15 relative, measured on tests/roundtrip/matmul_products.f90), which is more than
// machine precision, so a program that reaches the fallback earns neither VERDICT: equivalent nor VERDICT: identical algorithm.
// Set FORTRANBRIDGE_NO_LIBGFORTRAN=1 to force the fallback. Needs <dlfcn.h>, <cstdlib>, <vector>; link with -ldl on old glibc.
struct FortranDim { std::ptrdiff_t stride, lbound, ubound; };
struct FortranDType { std::size_t elem_len; int version; signed char rank, type; short attribute; };
template <int R> struct FortranArray {
    void* base_addr; std::size_t offset; FortranDType dtype; std::ptrdiff_t span; FortranDim dim[R];
};

template <int R> inline FortranArray<R> fortran_descriptor(void* base, std::size_t size, const std::ptrdiff_t* extent) {
    FortranArray<R> d{};
    d.base_addr = base;
    d.dtype.elem_len = size; d.dtype.rank = R; d.dtype.type = 3;
    d.span = static_cast<std::ptrdiff_t>(size);
    std::ptrdiff_t stride = 1;
    for (int k = 0; k < R; ++k) { d.dim[k].stride = stride; d.dim[k].lbound = 0; d.dim[k].ubound = extent[k] - 1; stride *= extent[k]; }
    return d;
}

inline void* fortran_library_routine(const char* name) {
    static void* library = [] () -> void* {
        const char* off = std::getenv("FORTRANBRIDGE_NO_LIBGFORTRAN");
        if (off && *off) return nullptr;
        for (const char* file : {"libgfortran.so.5", "libgfortran.so.4", "libgfortran.so.3"})
            if (void* handle = dlopen(file, RTLD_NOW | RTLD_LOCAL)) return handle;
        return nullptr;
    }();
    return library ? dlsym(library, name) : nullptr;
}

using FortranMatmul = void (*)(void*, void*, void*, int, int, void*);
template <class T> inline FortranMatmul fortran_matmul_routine() { return nullptr; }
template <> inline FortranMatmul fortran_matmul_routine<double>() { return reinterpret_cast<FortranMatmul>(fortran_library_routine("_gfortran_matmul_r8")); }
template <> inline FortranMatmul fortran_matmul_routine<float>() { return reinterpret_cast<FortranMatmul>(fortran_library_routine("_gfortran_matmul_r4")); }

// Column-order copies, as the library wants them.
template <class T> inline std::vector<T> fortran_columns(const std::vector<std::vector<T>>& a) {
    std::size_t n = a.size(), m = a.empty() ? 0 : a[0].size();
    std::vector<T> out(n * m);
    for (std::size_t j = 0; j < m; ++j) for (std::size_t i = 0; i < n; ++i) out[i + j * n] = a[i][j];
    return out;
}

template <class T>
inline std::vector<std::vector<T>> fortran_matmul(const std::vector<std::vector<T>>& a, const std::vector<std::vector<T>>& b) {
    std::size_t n = a.size(), inner = b.size(), m = b.empty() ? 0 : b[0].size();
    std::vector<std::vector<T>> c(n, std::vector<T>(m, T(0)));
    if (FortranMatmul routine = fortran_matmul_routine<T>()) {
        std::vector<T> ca = fortran_columns(a), cb = fortran_columns(b), out(n * m);
        std::ptrdiff_t ea[2] = {static_cast<std::ptrdiff_t>(n), static_cast<std::ptrdiff_t>(inner)}, eb[2] = {ea[1], static_cast<std::ptrdiff_t>(m)}, ec[2] = {ea[0], eb[1]};
        auto da = fortran_descriptor<2>(ca.data(), sizeof(T), ea), db = fortran_descriptor<2>(cb.data(), sizeof(T), eb), dc = fortran_descriptor<2>(out.data(), sizeof(T), ec);
        routine(&dc, &da, &db, 0, 30, nullptr);
        for (std::size_t j = 0; j < m; ++j) for (std::size_t i = 0; i < n; ++i) c[i][j] = out[i + j * n];
        return c;
    }
    for (std::size_t i = 0; i < n; ++i)
        for (std::size_t j = 0; j < m; ++j) {
            T s = 0;
            for (std::size_t k = 0; k < inner; ++k) s += a[i][k] * b[k][j];
            c[i][j] = s;
        }
    return c;
}

template <class T>
inline std::vector<T> fortran_matmul(const std::vector<std::vector<T>>& a, const std::vector<T>& v) {
    std::vector<T> c(a.size(), T(0));
    if (FortranMatmul routine = fortran_matmul_routine<T>()) {
        std::vector<T> ca = fortran_columns(a), cv = v;
        std::ptrdiff_t ea[2] = {static_cast<std::ptrdiff_t>(a.size()), static_cast<std::ptrdiff_t>(v.size())}, ev[1] = {ea[1]}, ec[1] = {ea[0]};
        auto da = fortran_descriptor<2>(ca.data(), sizeof(T), ea); auto dv = fortran_descriptor<1>(cv.data(), sizeof(T), ev); auto dc = fortran_descriptor<1>(c.data(), sizeof(T), ec);
        routine(&dc, &da, &dv, 0, 30, nullptr);
        return c;
    }
    for (std::size_t i = 0; i < a.size(); ++i) {
        T s = 0;
        for (std::size_t k = 0; k < v.size(); ++k) s += a[i][k] * v[k];
        c[i] = s;
    }
    return c;
}

template <class T>
inline std::vector<T> fortran_matmul(const std::vector<T>& v, const std::vector<std::vector<T>>& b) {
    std::size_t m = b.empty() ? 0 : b[0].size();
    std::vector<T> c(m, T(0));
    if (FortranMatmul routine = fortran_matmul_routine<T>()) {
        std::vector<T> cv = v, cb = fortran_columns(b);
        std::ptrdiff_t ev[1] = {static_cast<std::ptrdiff_t>(v.size())}, eb[2] = {ev[0], static_cast<std::ptrdiff_t>(m)}, ec[1] = {eb[1]};
        auto dv = fortran_descriptor<1>(cv.data(), sizeof(T), ev); auto db = fortran_descriptor<2>(cb.data(), sizeof(T), eb); auto dc = fortran_descriptor<1>(c.data(), sizeof(T), ec);
        routine(&dc, &dv, &db, 0, 30, nullptr);
        return c;
    }
    for (std::size_t j = 0; j < m; ++j) {
        T s = 0;
        for (std::size_t k = 0; k < v.size(); ++k) s += v[k] * b[k][j];
        c[j] = s;
    }
    return c;
}
