#ifndef SV_RUNTIME_HPP
#define SV_RUNTIME_HPP

#include <cstdint>
#include <cstdbool>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdarg>
#include <string>
#include <vector>
#include <array>
#include <algorithm>
#include <iostream>

#include "plusargs.h"
#include "mem_file.h"

/* --- SystemVerilog Multidimensional Fixed-Array Container Templates --- */
template<typename T, size_t D1>
struct Array1D {
    T data[D1] = {};
    T& operator[](size_t i) { return data[i]; }
    const T& operator[](size_t i) const { return data[i]; }
    Array1D& operator=(const T (&src)[D1]) { std::memcpy(data, src, sizeof(data)); return *this; }
};

template<typename T, size_t D1, size_t D2>
struct Array2D {
    T data[D1][D2] = {};
    T* operator[](size_t i) { return data[i]; }
    const T* operator[](size_t i) const { return data[i]; }
    Array2D& operator=(const T (&src)[D1][D2]) { std::memcpy(data, src, sizeof(data)); return *this; }
};

template<typename T, size_t D1, size_t D2, size_t D3>
struct Array3D {
    T data[D1][D2][D3] = {};
    auto operator[](size_t i) { return data[i]; }
    const auto operator[](size_t i) const { return data[i]; }
};

/* --- SystemVerilog Random Number System Tasks ($urandom, $urandom_range, $srandom) --- */
uint32_t sv_urandom(void);
uint32_t sv_urandom_range(uint32_t max_val, uint32_t min_val = 0);
void sv_srandom(uint32_t seed);

/* --- SystemVerilog String Formatting System Tasks ($sformat, $sformatf) --- */

/* $sformat: Task form, formats string in-place into output variable */
void sv_sformat(std::string &out_str, const char *fmt, ...);
void sv_sformat(char *out_buf, const char *fmt, ...);

template<size_t N>
inline void sv_sformat(char (&out_buf)[N], const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    std::vsnprintf(out_buf, N, fmt, args);
    va_end(args);
}

template<size_t N>
inline void sv_sformat(uint8_t (&out_buf)[N], const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    std::vsnprintf(reinterpret_cast<char*>(out_buf), N, fmt, args);
    va_end(args);
}

/* $sformatf: Function form, returns formatted std::string */
std::string sv_sformatf(const char *fmt, ...);

/* --- Plusargs System Tasks ($value$plusargs) --- */
int sv_value_plusargs(const char *fmt, std::string &out_str);

template<typename T>
inline int sv_value_plusargs(const char *fmt, T &out_val) {
    return ::sv_value_plusargs(fmt, &out_val);
}

/* --- Bare-metal Memory File API std::string Overloads --- */
int mem_fopen(const std::string &path, const char *mode);
int mem_fopen(const std::string &path, const std::string &mode);
int mem_fopen(const char *path, const std::string &mode);

#endif /* SV_RUNTIME_HPP */
